import logging
import time
from pathlib import Path
from src.services.board import claim_board_job, execute_board_job
from src.services.execution import claim, execute, materialize_due
from src.utils.time import now
from src.utils.wakeup import wait

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
heartbeat = Path('/tmp/analysis-heartbeat')


def main():
    while True:
        try:
            materialize_due()
            lease = claim()
            # 每轮最多领取一个看板任务，执行完下一轮再领，防止并发租约互相覆盖。
            board_context = None if lease else claim_board_job(now())
            heartbeat.touch()
            if lease:
                execute(lease)
                heartbeat.touch()
            if board_context:
                reason = execute_board_job(board_context, now())
                if reason:
                    log.warning('Board job %s not published: %s', board_context['job_id'], reason)
                heartbeat.touch()
            if not lease and not board_context:
                wait()
        except Exception:
            log.exception('Worker cycle failed')
            time.sleep(2)


if __name__ == '__main__':
    main()
