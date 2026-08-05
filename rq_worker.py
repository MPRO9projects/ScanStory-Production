import os


def main():
    if not os.environ.get("REDIS_URL"):
        raise SystemExit("REDIS_URL is required for the RQ worker.")
    from redis import Redis
    from rq import Queue, Worker
    import app as app_module

    queue_name = os.environ.get("RQ_QUEUE_NAME", "scanstory-processing")
    with app_module.app.app_context():
        connection = Redis.from_url(os.environ["REDIS_URL"])
        worker = Worker([Queue(queue_name, connection=connection)], connection=connection)
        worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
