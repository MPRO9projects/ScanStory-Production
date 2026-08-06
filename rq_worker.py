import os


def main():
    from redis import Redis
    from rq import Queue, Worker
    import app as app_module
    from processing_queue import QueueUnavailable, queue_config_summary

    try:
        config = queue_config_summary()
    except QueueUnavailable as exc:
        raise SystemExit(str(exc))
    if config["mode"] != "rq":
        raise SystemExit("RQ worker requires SCANSTORY_QUEUE_MODE=rq.")
    if not os.environ.get("REDIS_URL"):
        raise SystemExit("REDIS_URL is required for the RQ worker.")
    with app_module.app.app_context():
        app_module.app.logger.info(
            "rq_worker_starting",
            extra={"processing_worker": {
                "queue_name": config["queue_name"],
                "timeout_seconds": config["timeout_seconds"],
                "redis_configured": True,
            }},
        )
        connection = Redis.from_url(os.environ["REDIS_URL"])
        worker = Worker([Queue(config["queue_name"], connection=connection)], connection=connection)
        worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
