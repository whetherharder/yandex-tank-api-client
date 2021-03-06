"""
Yandex.Tank API coroutine client capable of multi-tank multi-config tests.

Note: HTTP requests issued by this module are blocking
(usually they are small and are processed quickly).
"""
import time
import fnmatch
import os.path
import logging
import urllib2
import yaml
import functools as ft
logger = logging.getLogger(__name__)  # pylint:disable=C0103

from trollius import coroutine, sleep, Return,\
    CancelledError, From, gather, async, Condition

import yandex_tank_api_client.session as tankapi


@coroutine
def shoot(cfgs, status_callback):
    """
    Performs multi-tank multi-config test.
    Accepts one or more config dicts.
    Returns list of session ID's.
    Raises TankLocked and TestFailed.
    """

    try:
        sessions = [
            SessionWrapper(ft.partial(  # pylint: disable=W0142
                status_callback, i), **cfg)
            for i, cfg in enumerate(cfgs)]
    except Exception:
        logger.exception("Failed to initialize session objects, config:\n%s",
                         yaml.safe_dump(cfgs))
        raise
    prepares = []
    runs = []
    stops = []
    try:
        try:
            prepares = [async(session.prepare()) for session in sessions]
            yield From(gather(*prepares))  # pylint: disable=W0142
            logger.info("All tanks are prepared. STARTING TO SHOOT.")
            runs = [async(session.run_until_finish()) for session in sessions]
            yield From(gather(*runs))  # pylint: disable=W0142
        except KeyboardInterrupt:
            logger.info("Test interrupted")
            raise
        except CancelledError:
            logger.info("Test cancelled")
            raise
        except TestFailed:
            logger.info("Test failed")
            raise
        except Exception:
            logger.exception("Exception occured in Test.run_until_finish()")
            raise
        except BaseException:
            logger.exception(
                "Something strange caught by Test.run_until_finish()")
            raise
    except BaseException as ex:
        logger.info("Stopping remaining tank sessions...")
        stops = [async(session.stop())
                 for session in sessions if not session.finished]
        yield From(gather(*stops, return_exceptions=True))  # pylint: disable=W0142
        raise ex
    finally:
        for task in prepares + runs + stops:
            task.cancel()
		
    logger.info("All tanks are done.")
    raise Return([
        session.session.s_id
        if session.session is not None else None
        for session in sessions
    ])


class TankLocked(RuntimeError):

    """Raised when another session is running or tank lock is present"""
    pass


class TestFailed(RuntimeError):

    """Raised when test status indicates an irrecoverable failure"""

    def __init__(self, status):
        failures = '\n'.join(
            ("================\n"
                "Stage: %s\n"
                "Reason:\n%s\n") %
            (flr.get('stage', '-'), flr.get('reason', '-'))
            for flr in status.get('failures', [])
        )
        msg = ("Test failed:\n"
               "retcode=%s\n"
               "test=%s\n"
               "Failures:\n%s\n"
               "===============\n") % (status.get('retcode', '-'),
                                       status.get('test', '-'),
                                       failures)
        RuntimeError.__init__(self, msg)
        self.status = status


class SessionWrapper(object):
    # pylint: disable=R0902

    """
    prepare():
        obtains tank, uploads files
        and returns when 'prepare' stage is completed
    run_until_finish():
        continues test, downloads artifacts and waits for finish
    stop():
        stops test at arbitratry point
    """

    def __init__(self, status_callback, **params):
        # pylint: disable=R0912
        # pylint: disable=R0915

        self.status_cond = Condition()
        self.poll_loop_task = None

        self.status = dict()
        self.status_changes = set()
        self.session = None
        self.finished = False
        self.status_callback = status_callback
        log_name = params.get('log_name', params.get(
            'options', {}).get('meta.job_name', 'tank'))
        self.log = logger.getChild(log_name)
        try:

            self.tanks = params.get('tanks', [])
            if 'tank' in params:
                self.tanks.append(params['tank'])
            options = []
            for key, value in params.get('options', {}).iteritems():
                if value is None:
                    value = ''
                options.append((key, value))

            if 'config' in params and params['config']:
                if isinstance(params['config'], str):
                    config_files = [params['config']]
                elif isinstance(params['config'], list):
                    config_files = params['config']
                else:
                    raise ValueError("Bad config entry")
            self.tank_config = '\n\n'.join(
                open(cnf).read() for cnf in config_files)
            self.tank_config += tankapi.make_ini(options)

            self.download_list = params.get('download', [])
            self.upload = []
            for entry in params.get('upload', []):
                if isinstance(entry, str):
                    _, target_file = os.path.split(entry)
                    self.upload.append((entry, target_file))
                else:
                    try:
                        if len(entry) != 2:
                            raise ValueError("")
                    except:
                        raise ValueError(
                            "Malformed upload section: " + str(entry))
                    else:
                        self.upload.append(entry)

            self.start_timeout = params.get('start_timeout', 14400)
            self.expected_codes = params.get('expected_codes', [0])
            self.artifacts_by_session = params.get(
                'artifacts_by_session', False)
        except Exception:
            self.log.exception("Failed to initialize Session object")
            raise
        try:
            _ = [int(code) for code in self.expected_codes]
        except ValueError:
            raise ValueError(
                'expected_codes should be an iterable of INTEGERS')
        except TypeError:
            raise ValueError(
                'expected_codes should be an ITERABLE of integers')

    def __del__(self):
        if self.poll_loop_task is not None:
            self.poll_loop_task.cancel()
 	
    @coroutine
    def prepare(self):
        """
        Obtain tank session on some tank
        Return when it passes 'prepare' stage
        """
        start_time = time.time()
        while True:
            for tank in self.tanks:
                try:
                    yield From(self._prepare_tank(tank))
                except tankapi.RetryLater:
                    self.log.info("%s is either absent or locked", tank)
                    continue
                self.log.info("Tank %s is ready to start test", tank)
                raise Return()
            wait_time = time.time() - start_time
            if wait_time > self.start_timeout:
                raise TankLocked(
                    "Tank locked, failed to start test in %d seconds" %
                    wait_time)
            self.log.info("All allowed tanks are locked, waiting 5 seconds...")
            yield From(sleep(5))

    @coroutine
    def run_until_finish(self):
        """
        Wait for postprocess, download artifacts and finalize session (if any)
        """
        try:
            self.session.set_breakpoint("unlock")
        except tankapi.NothingDone as err:
            self.log.warn(str(err))

        self.log.info("Waiting for session  %s at tank %s to stop",
                      self.session.s_id, self.session.tank)

        yield From(self._run_until_stage_completion('postprocess'))

        try:
            self._download_artifacts()
            try:
                self.session.set_breakpoint('finished')
            except tankapi.APIError as api_err:
                if api_err.get('status', '--unknown--')\
                        not in ('success', 'failed'):
                    raise
            yield From( self._run_until_stage_completion())
            if self.status['status'] == 'success' and\
                    (self.status['retcode'] is not None and
                     int(self.status['retcode']) in self.expected_codes):
                self.log.info("Test succeded")
                raise Return()
        except tankapi.APIError:
            self.log.warning(
                "Failed to finalize session %s on tank %s",
                self.session.s_id, self.session.tank, exc_info=True)

        self.log.warning("Session %s on tank %s failed",
                         self.session.s_id, self.session.tank)
        raise TestFailed(self.status)

    @coroutine
    def stop(self, wait=True):
        """
        Stops current session, if any.
        Catches all exceptions from tankapi client.
        """
        if self.session is None:
            self.log.debug("Cannot stop: no session obtained")
            raise Return()
        if self.finished:
            self.log.debug("Session %s on tank %s has already finished",
                           self.session.s_id, self.session.tank)
            raise Return()
        n_stop_attempts = 0
        while True:
            self.log.info("Asking tank to stop session %s", self.session.s_id)
            try:
                self.session.stop()
            except tankapi.NothingDone:
                self.log.info("Session %s is not running", self.session.s_id)
            except urllib2.URLError:
                self.log.exception(
                    "Failed to communicate with %s to stop session",
                    self.session.tank)
                n_stop_attempts += 1
                if n_stop_attempts < 5:
                    yield From(sleep(5))
                else:
                    self.poll_loop_task.cancel()
                    raise
            except:
                self.log.critical("Failed to stop session %s at tank %s, leaving in in unknown state",
                                  self.session.s_id, self.session.tank,
                                  exc_info=True)
                self.poll_loop_task.cancel()
                raise
            else:
                break

        if wait:
            yield From(self.run_until_finish())

    @coroutine
    def _prepare_tank(self, tank):
        """
        Return tankapi.Session for acquired tank
        Raises tankapi.RetryLater if tank is busy
        Should not be called after some session was successfully acquired
        """
        first_break = 'configure' if self.upload else 'start'
        self.log.info("Trying to start session at %s ...", tank)
        try:
            self.session = tankapi.Session(
                tank=tank,
                config_contents=self.tank_config,
                stage=first_break
            )
        except urllib2.URLError as exc:
            self.log.warning(
                "Failed to communicate with %s", tank, exc_info=True)
            raise tankapi.RetryLater(str(exc), {})
        else:
            if self.poll_loop_task is None:
                self.poll_loop_task = async(self._poll_loop())
        self.log.info("Started session %s", self.session.s_id)
        if self.upload:
            yield From(self._run_until_stage_completion('lock'))
            for local_path, remote_name in self.upload:
                self.session.upload(local_path, remote_name)
            self.session.set_breakpoint('start')
        yield From(self._run_until_stage_completion('prepare'))

    def _download_artifacts(self):
        """Downloads files by mask into specified dir"""
        self.log.info("Downloading artifacts for session  %s from tank %s",
                      self.session.s_id, self.session.tank)

        if self.artifacts_by_session:
            artifact_dir = self.session.s_id
            try:
                os.makedirs(self.session.s_id)
            except OSError as err:
                self.log.warning(
                    "Failed to create artifact directory %s: %s",
                    self.session.s_id, str(err))
                return
        else:
            artifact_dir = '.'
        try:
            artifacts = self.session.get_artifact_list()
        except tankapi.APIError:
            self.log.error("Failed to obtain artifact list: %s", str(err))
            return
        except urllib2.URLError:
            self.log.exception("Failed to obtain artifact list")
            return

        for art in artifacts:
            if any(fnmatch.fnmatch(art, patt) for patt in self.download_list):
                try:
                    self.log.info("Downloading %s from %s",
                                  art, self.session.tank)
                    self.session.download_artifact(
                        art, os.path.join(artifact_dir, art))
                except urllib2.URLError:
                    self.log.exception(
                        "Failed to download %s from %s", art, self.session.tank)
                except tankapi.APIError:
                    self.log.exception(
                        "Failed to download %s from %s", art, self.session.tank)

    @coroutine
    def _poll_loop(self, poll_interval=5):
        """
        Poll current session stattus in a loop until finished
        """
        while not self.finished:
            new_status = yield From(self._get_status())
            yield From(self._handle_status_update(new_status))
            yield From(sleep(poll_interval))
        self.log.info("Test finished, stopping to poll tank status.")

    @coroutine
    def _get_status(self, poll_interval=5, attempts=6):
        """
        :returns: status or None if all attempt failed
        """
        for attempt_no in xrange(attempts):
            try:
                status = self.session.get_status()
            except urllib2.URLError as err:
                if attempt_no >= (attempts - 1):
                    self.log.error(
                        "All attempts to obtain session status failed: %s",
                        str(err)
                        )
                    raise Return(None)
                self.log.warning(
                    "Failed to obtain session status: %s", str(err))
                yield From(sleep(poll_interval))
            else:
                raise Return(status)

    @coroutine
    def _handle_status_update(self, status):
        """
        Update self.status, check for difference
        and notify coroutines waiting for status change.
        """
        self.status_changes = set()
        if status is None:
            self.status_changes.add('poll_error')
        else:
            if status != self.status and self.status_callback:
                self.status_callback(status)
            for key in ('status', 'current_stage',
                    'stage_completed', 'failures'):
                if status.get(key) != self.status.get(key):
                    self.status_changes.add(key)
            self.status = status
        if self.status_changes:
            self.log.info("Status changes: %s",', '.join(self.status_changes))		
            self.log.debug("New status:\n%s", self.status)
            yield From(self.status_cond.acquire())
            self.status_cond.notify_all()
            self.status_cond.release()
        else:
            self.log.debug("Alive, no important status changes")

    @coroutine
    def _run_until_stage_completion(self, target_stage=None):
        """
        Waits either for test success or for completion of a patrticular stage.
        Raises:
            tankapi.RetryLater if tank lock is found
            TestFailed
        """
        while not self._has_completed(target_stage):
            yield From(self.status_cond.acquire())
            yield From(self.status_cond.wait())
            self.status_cond.release()

    def _has_completed(self, target_stage):
        """
        :returns bool: True if stage/all test completed
        :raises  tankapi.RetryLater: if tank poll failed before ttank was locked
        :raises  tankapi.RetryLater: if tank lock is found
        :raises  TestFailed: if tank reports test failure
        :raises  RuntimeError: in case of irrecoverable things
        """
        if 'poll_error' in self.status_changes:
            if not self.status or\
                self.status['current_stage'] in ('init', 'lock'):
                # We have not locked the tank yet
                raise tankapi.RetryLater()
            # We have locked the tank and it died quietly
            raise RuntimeError("Complete tank status poll failure")

        if 'failures' in self.status_changes and \
                any(flr['stage'] == 'lock'
                    for flr in self.status['failures']):
            self.log.info("%s is locked", self.session.tank)
            raise tankapi.RetryLater()

        if 'status' in self.status_changes:
            if self.status['status'] == 'failed':
                self.finished = True
                self.log.warning(
                    "Session %s on %s failed:\n%s",
                    self.session.s_id,
                    self.session.tank,
                    '\n'.join('%s: %s' % (
                        flr.get('stage', '__unknown stage__'),
                        flr.get('reason', '__reason not specified__')
                    )
                        for flr in self.status.get('failures', [])
                    )
                )
                raise TestFailed(self.status)

            if self.status['status'] == 'success':
                self.finished = True
                self.log.info("Session %s finished successfully",
                              self.session.s_id)
                return True

        last_stage = self.status.get('current_stage', 'unknown')
        completed = self.status.get('stage_completed', False)
        self.log.info(
            "Session %s: %s, %scomplete",
            self.session.s_id,
            last_stage,
            '' if completed else 'in'
        )
        return completed and target_stage is not None and (target_stage == last_stage)
