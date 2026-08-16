"""Account storage accounting (V1.1 Wave 3).

Wave 2 made `base_storage_bytes` a real ENTITLEMENT number with no usage behind
it. This module is the usage half: the authoritative MediaObject ledger, the
concurrency-safe reservation primitive, and the pure policy predicates that
decide whether a create/replace/transfer may consume storage.

TWO VALUES, ONE TRUTH
---------------------
`media_objects` is the authoritative AUDIT ledger - one row per retained,
customer-uploaded file, with the bytes actually on disk.

`users.storage_used_bytes` is the ENFORCEMENT value. Quota decisions happen
inside one conditional UPDATE against that column, which is the only way to
avoid "read SUM -> compare -> two uploads both proceed". This is deliberately
the same split app.py already documents for project capacity (ledger = audit,
materialized column = enforcement), and `flask reconcile-storage` re-derives
the column from the ledger, so drift is detectable and repairable rather than
permanent.

IMPORT DIRECTION
----------------
This module imports models only. entitlements.py imports THIS module (never the
reverse), so the effective allowance is always passed IN as a parameter rather
than re-resolved here - which also keeps every predicate below pure and
directly unit-testable.
"""
from sqlalchemy import case
from sqlalchemy.sql import func

from models import MediaObject, User, db, get_utc_now


MEDIA_ROLE_TRIGGER_IMAGE = "trigger_image"
MEDIA_ROLE_VIDEO = "video"


# ---------------------------------------------------------------------------
# Ledger reads
# ---------------------------------------------------------------------------
def _counted(query):
    return query.filter(
        MediaObject.status == "ACTIVE",
        MediaObject.counts_toward_quota.is_(True),
    )


def account_storage_used_bytes(user_id):
    """Authoritative ledger SUM for one account. The AUDIT number.

    Enforcement reads users.storage_used_bytes instead; this is what
    reconciliation compares that column against.
    """
    if not user_id:
        return 0
    total = _counted(
        db.session.query(func.coalesce(func.sum(MediaObject.size_bytes), 0)).filter(
            MediaObject.owner_user_id == user_id
        )
    ).scalar()
    return int(total or 0)


def stored_storage_used_bytes(user):
    """The materialized enforcement counter."""
    return int(getattr(user, "storage_used_bytes", 0) or 0) if user else 0


def project_counted_bytes(project_id):
    """Bytes this project would carry with it on an ownership transfer."""
    if not project_id:
        return 0
    total = _counted(
        db.session.query(func.coalesce(func.sum(MediaObject.size_bytes), 0)).filter(
            MediaObject.project_id == project_id
        )
    ).scalar()
    return int(total or 0)


def active_media_objects(project_id=None, pair_id=None, storage_key=None):
    query = MediaObject.query.filter(MediaObject.status == "ACTIVE")
    if project_id is not None:
        query = query.filter(MediaObject.project_id == project_id)
    if pair_id is not None:
        query = query.filter(MediaObject.pair_id == pair_id)
    if storage_key is not None:
        query = query.filter(MediaObject.storage_key == storage_key)
    return query.all()


def active_media_object_for_key(storage_key):
    return MediaObject.query.filter(
        MediaObject.storage_key == storage_key,
        MediaObject.status == "ACTIVE",
    ).first()


# ---------------------------------------------------------------------------
# Ledger writes. None of these commit - they join the caller's transaction so a
# rollback releases the accounting exactly like it releases a project slot.
# ---------------------------------------------------------------------------
def record_media_object(storage_key, size_bytes, media_role, owner_user_id=None,
                        owner_admin_id=None, project_id=None, pair_id=None,
                        source="upload", counts_toward_quota=True):
    """Add one ACTIVE ledger row. Caller reserves the bytes first."""
    obj = MediaObject(
        owner_user_id=owner_user_id,
        owner_admin_id=owner_admin_id,
        project_id=project_id,
        pair_id=pair_id,
        media_role=media_role,
        storage_key=storage_key,
        size_bytes=int(size_bytes or 0),
        counts_toward_quota=bool(counts_toward_quota),
        status="ACTIVE",
        source=source,
    )
    db.session.add(obj)
    return obj


def supersede_media_object(obj):
    """Retire a row whose bytes were overwritten in place by a replacement.

    Not a deletion: the physical file at this key still exists, it just holds
    different content now, and the NEW ACTIVE row accounts for those bytes.
    """
    if obj is None or obj.status != "ACTIVE":
        return obj
    obj.status = "SUPERSEDED"
    obj.superseded_at = get_utc_now()
    return obj


def mark_media_object_deleted(obj):
    """Free a row's bytes. ONLY call after the physical unlink SUCCEEDED."""
    if obj is None or obj.status == "DELETED":
        return obj
    obj.status = "DELETED"
    obj.deleted_at = get_utc_now()
    return obj


# ---------------------------------------------------------------------------
# Concurrency-safe reservation
# ---------------------------------------------------------------------------
def reserve_account_storage(user_id, delta_bytes, allowance_bytes):
    """Atomically consume `delta_bytes` if the allowance permits. True on success.

    One conditional UPDATE, no read-then-write - the same primitive shape as
    app._atomic_increment_user_counter. Two concurrent uploads serialize on the
    user row: the loser re-evaluates its WHERE against the winner's committed
    value and fails, so the pair can never both fit into one slot's worth of
    headroom. `allowance_bytes=None` means no storage limit is configured.
    """
    delta = int(delta_bytes or 0)
    if delta < 0:
        release_account_storage(user_id, -delta)
        return True
    if delta == 0:
        return True
    used = func.coalesce(User.storage_used_bytes, 0)
    query = User.query.filter(User.id == user_id)
    if allowance_bytes is not None:
        query = query.filter(used + delta <= int(allowance_bytes))
    updated = query.update({User.storage_used_bytes: used + delta}, synchronize_session=False)
    return updated == 1


def release_account_storage(user_id, delta_bytes):
    """Give bytes back (failed upload, deletion, transfer-out). Never negative."""
    delta = int(delta_bytes or 0)
    if delta <= 0 or not user_id:
        return
    used = func.coalesce(User.storage_used_bytes, 0)
    User.query.filter(User.id == user_id).update(
        {User.storage_used_bytes: case((used < delta, 0), else_=used - delta)},
        synchronize_session=False,
    )


# ---------------------------------------------------------------------------
# Policy predicates (pure - no session, no I/O)
# ---------------------------------------------------------------------------
def can_consume(used_bytes, allowance_bytes, new_bytes):
    """New consumption: blocked outright while over storage."""
    if allowance_bytes is None:
        return True
    if int(new_bytes or 0) <= 0:
        return True
    return int(used_bytes or 0) + int(new_bytes) <= int(allowance_bytes)


def evaluate_replacement(used_bytes, allowance_bytes, old_bytes, new_bytes):
    """(allowed, projected_usage) for swapping old_bytes of media for new_bytes.

    Two independent gates, both of which must pass; this is only the STORAGE
    one. Per-file policy (image/video bytes, duration, dimensions, pixels) is
    checked separately by the upload validators and a storage allowance never
    substitutes for it.

    Within allowance: projected final usage must fit the allowance.
    Over allowance (valid after a downgrade / refund / grant revocation): the
    swap must STRICTLY REDUCE total usage. Equal-size and larger are blocked;
    a genuinely smaller replacement is allowed even if it lands still over.
    """
    used = int(used_bytes or 0)
    projected = used - int(old_bytes or 0) + int(new_bytes or 0)
    if allowance_bytes is None:
        return True, projected
    if used > int(allowance_bytes):
        return projected < used, projected
    return projected <= int(allowance_bytes), projected


def evaluate_storage_transfer(project_id, recipient_used_bytes, recipient_allowance_bytes):
    """Can the recipient absorb this project's counted bytes?

    Returns (ok, project_bytes). Callable by whichever future checkpoint adds
    the ownership-transfer HTTP surface - it takes plain numbers so it needs no
    request context.
    """
    project_bytes = project_counted_bytes(project_id)
    return can_consume(recipient_used_bytes, recipient_allowance_bytes, project_bytes), project_bytes


def move_project_storage_ownership(project_id, from_user_id, to_user_id):
    """Move a project's storage responsibility with its ownership.

    Joins the CALLER's transaction on purpose: ownership and accounting must
    move together or not at all, never as a second step that can half-fail.
    Returns the bytes moved.
    """
    project_bytes = project_counted_bytes(project_id)
    MediaObject.query.filter(
        MediaObject.project_id == project_id,
        MediaObject.status == "ACTIVE",
    ).update({MediaObject.owner_user_id: to_user_id}, synchronize_session=False)
    if project_bytes:
        release_account_storage(from_user_id, project_bytes)
        # Unconditional: the capacity check already happened in the same
        # transaction, and a recipient must never end up owning media that is
        # not counted against them.
        used = func.coalesce(User.storage_used_bytes, 0)
        User.query.filter(User.id == to_user_id).update(
            {User.storage_used_bytes: used + project_bytes}, synchronize_session=False
        )
    return project_bytes
