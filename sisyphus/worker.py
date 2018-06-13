import gzip
import logging
import os
import pickle
import pprint
import psutil
import pwd
import signal
import socket
import time
from threading import Thread

import sisyphus.global_settings as gs
engine = gs.cached_engine


def format_time(t):
    minutes, seconds = divmod(int(t), 60)
    hours, minutes = divmod(minutes, 60)
    return "%d:%02d:%02d" % (hours, minutes, seconds)


def format_bytes(b):
    return format_number(b,
                         factor=1024,
                         mapping=['B', 'kB', 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB' 'YB'],
                         use_decimal_after=3)


def format_number(number,
                  factor=1000,
                  mapping=[''] + ['e+%02i' % i for i in range(3, 31, 3)],
                  use_decimal_after=1):
    if use_decimal_after is None:
        use_decimal_after = len(mapping)
    residual = 0
    count = 0
    result = number
    while result >= factor and count < len(mapping) - 1:
        result, residual = divmod(result, factor)
        count += 1
    if count < use_decimal_after:
        return "%i%s" % (result, mapping[count])
    else:
        return "%.2f%s" % ((result*factor+residual)/factor, mapping[count])


class LoggingThread(Thread):
    """ Thread to log memory and time consumtion of job
    """

    def __init__(self, job, task, task_id, engine_selector):
        self.job = job
        self.task = task
        self.task_id = task_id
        self.start_time = None
        self.engine_selector = engine_selector
        super().__init__()
        self.out_of_memory = False
        self.__stop = False
        self.rqmt = engine().get_rqmt(task, task_id, update=False)

    def run(self):
        start_time = time.time()
        last_rss = None
        current_process = psutil.Process(os.getpid())
        max_resources = {}

        usage_file = open(self.task.get_process_logging_path(self.task_id), 'w')

        def log_usage(current):
            usage_file.seek(0)

            usage = {'max': max_resources,
                     'current': current,
                     'pid': os.getpid(),
                     'user': pwd.getpwuid(os.geteuid()).pw_name,
                     'used_time': (time.time()-start_time) / 3600.,
                     'host': socket.gethostname(),
                     'current_time': time.ctime(),
                     'out_of_memory': self.out_of_memory,
                     'requested_resources': self.rqmt}
            usage_file.write("%s\n" % pprint.pformat(usage))
            usage_file.truncate()
            usage_file.flush()

        last_log_value = 0
        last_log_time = 0
        max_resources = resources = engine().get_job_used_resources(current_process, self.engine_selector)
        while not self.__stop:
            resources = engine().get_job_used_resources(current_process, self.engine_selector)
            # Only print log if rss changed at least bey PLOGGING_MIN_CHANGE
            if last_rss is None or abs(last_rss - resources['rss'])/last_rss > gs.PLOGGING_MIN_CHANGE:
                if not gs.PLOGGING_QUIET:
                    logging.info("Run time: {time} CPU: {cpu:.2f}% RSS: {rss} VMS: {vms}".format(
                                 time=format_time(time.time()-start_time),
                                 cpu=resources['cpu'],
                                 rss=format_bytes(resources['rss']*1024**3),
                                 vms=format_bytes(resources['vms']*1024**3)))
                last_rss = resources['rss']

            # store max used resources
            for k, v in resources.items():
                c = max_resources.get(k)
                if c is None or c < v:
                    max_resources[k] = v

            # update log file:
            # at least every PLOGGING_UPDATE_FILE_PERIOD seconds or
            # if rss usage grow relative more then PLOGGING_MIN_CHANGE
            # if (max_resources['rss'] > last_log_value and time.time() - last_log_time > 30) or \
            if time.time() - last_log_time > gs.PLOGGING_UPDATE_FILE_PERIOD or\
                    (max_resources['rss'] - last_log_value)/last_log_value > gs.PLOGGING_MIN_CHANGE:
                log_usage(resources)
                last_log_value = max_resources['rss']
                last_log_time = time.time()
            time.sleep(gs.PLOGGING_INTERVAL)

            # if less then 2% or less then 256MB are free
            # if max_mem * 0.98 < last_rss:
            #     if max_mem and (max_mem - last_rss) / max_mem < 0.02 or max_mem - last_rss < 2**28:
            #     self.task.check_state(gs.JOB_CLOSE_TO_MAX_MEM, task_id=self.task_id, update=True)

        log_usage(resources)
        logging.info("Max resources: Run time: {time} CPU: {cpu}% RSS: {rss} VMS: {vms}"
                     "".format(time=format_time(time.time()-start_time),
                               cpu=max_resources['cpu'],
                               rss=format_bytes(max_resources['rss']*1024**3),
                               vms=format_bytes(max_resources['vms']*1024**3)))

    def stop(self):
        self.__stop = True
        self.join()


def worker(args):
    try:
        worker_helper(args)
    except Exception:
        task_id = engine().get_task_id(args.task_id, args.engine)
        error_file = "%s.%s.%i" % (args.jobdir + os.path.sep + gs.STATE_ERROR, args.task_name, task_id)
        if not os.path.isfile(error_file) and not os.path.isdir(error_file):
            # create error file
            with open(error_file, 'w') as f:
                pass
        raise


def worker_helper(args):
    """ This program is run on the client side when running the job """

    # ignore ctrl-c signal
    def catch_signal(signal_number, frame):
        pass
    signal.signal(signal.SIGINT, catch_signal)

    with gzip.open(os.path.join(args.jobdir, gs.JOB_SAVE)) as f:
        job = pickle.load(f)

    if not job._sis_runnable():
        for path in job._sis_inputs:
            if path.available():
                logging.info("Path available:     %s" % path)
            else:
                logging.error("Path not available: %s" % path)
        assert False, "Job isn't runnable, probably some inputs are not ready"
    # find task
    task = None
    for task_check in job._sis_tasks():
        if task_check.name() == args.task_name:
            task = task_check
            break

    assert task is not None, [t.name() for t in job._sis_tasks()]  # No task with that name found!

    # Work around for bug, the wrong job can be linked to the task
    # for some reason, this sets it back. TODO: find the real problem
    task._job = job

    task_id = engine().get_task_id(args.task_id, args.engine)
    logging.debug("Task id: %s" % str(task_id))
    logging_thread = LoggingThread(job, task, task_id, args.engine)
    logging_thread.start()

    resume_job = False

    # setup log file by linking to engine logfile
    logpath = os.path.relpath(task.path(gs.JOB_LOG, task_id))
    if os.path.isfile(logpath):
        if not args.force_resume:  # skip setting the resume flag to run unresumable jobs
            resume_job = True
        os.unlink(logpath)

    engine_logpath = engine().get_logpath(task.path(gs.JOB_LOG_ENGINE), task.name(), task_id, args.engine)
    try:
        if os.path.isfile(engine_logpath):
            os.link(engine_logpath, logpath)
        else:
            # e.g. LSF engine only creates this file after job terminates
            logging.warning("Could not find engine logfile: %s Create soft link anyway." % engine_logpath)
            os.symlink(os.path.relpath(engine_logpath, os.path.dirname(logpath)), logpath)
    except FileExistsError:
        pass

    # cleanup environment
    if hasattr(task._job, '_sis_environment'):
        task._job._sis_environment.modify_environment()

    # run task
    task.run(task_id, resume_job, logging_thread=logging_thread)