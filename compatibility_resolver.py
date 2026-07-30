from models import Experience, Project, ProjectPair, Trigger


def resolve_experience_for_legacy_project(project_id):
    project = Project.query.get(project_id)
    if not project:
        return None, None
    experience = Experience.query.filter_by(legacy_project_id=project.id).first()
    return project, experience


def resolve_trigger_for_legacy_pair(pair_id):
    pair = ProjectPair.query.get(pair_id)
    if not pair:
        return None, None
    trigger = Trigger.query.filter_by(legacy_project_pair_id=pair.id).first()
    return pair, trigger


def resolve_legacy_project_for_experience(experience_id):
    experience = Experience.query.get(experience_id)
    if not experience or not experience.legacy_project_id:
        return experience, None
    return experience, Project.query.get(experience.legacy_project_id)


def resolve_legacy_pair_for_trigger(trigger_id):
    trigger = Trigger.query.get(trigger_id)
    if not trigger or not trigger.legacy_project_pair_id:
        return trigger, None
    return trigger, ProjectPair.query.get(trigger.legacy_project_pair_id)
