# -*- coding: utf-8 -*-
'''
    python-capture-agent
    ~~~~~~~~~~~~~~~~~~~~

    :copyright: 2014-2017, Lars Kiesow <lkiesow@uos.de>
    :license: LGPL – see license.lgpl for more details.
'''

from pyca.utils import timestamp, try_mkdir, configure_service, ensurelist
from pyca.utils import set_service_status, set_service_status_immediate
from pyca.utils import recording_state, update_event_status, terminate
from pyca.config import config
from pyca.db import get_session, RecordedEvent, UpcomingEvent, Status,\
                    Service, ServiceStatus
import logging
import os
import os.path
import shlex
import signal
import subprocess
import sys
import time
import traceback

logger = logging.getLogger(__name__)
captureproc = None


def sigterm_handler(signum, frame):
    '''Intercept sigterm and terminate all processes.
    '''
    if captureproc and captureproc.poll() is None:
        captureproc.terminate()
    terminate(True)
    sys.exit(0)


def start_capture(upcoming_event):
    '''Start the capture process, creating all necessary files and directories
    as well as ingesting the captured files if no backup mode is configured.
    '''
    logger.info('Start recording')

    # First move event to recording_event table
    db = get_session()
    event = db.query(RecordedEvent)\
              .filter(RecordedEvent.uid == upcoming_event.uid)\
              .filter(RecordedEvent.start == upcoming_event.start)\
              .first()
    if not event:
        event = RecordedEvent(upcoming_event)
        db.add(event)
        db.commit()

    try_mkdir(config()['capture']['directory'])
    os.mkdir(event.directory())

    # Set state
    set_service_status_immediate(Service.CAPTURE, ServiceStatus.BUSY)
    recording_state(event.uid, 'capturing')
    update_event_status(event, Status.RECORDING)

    # Recording
    tracks = recording_command(event)
    event.set_tracks(tracks)
    db.commit()

    # Set status
    set_service_status_immediate(Service.CAPTURE, ServiceStatus.IDLE)
    update_event_status(event, Status.FINISHED_RECORDING)


def safe_start_capture(event):
    '''Start a capture process but make sure to catch any errors during this
    process, log them but otherwise ignore them.
    '''
    try:
        start_capture(event)
        return True
    except Exception:
        logger.error('Recording failed')
        logger.error(traceback.format_exc())
        # Update state
        recording_state(event.uid, 'capture_error')
        update_event_status(event, Status.FAILED_RECORDING)
        set_service_status_immediate(Service.CAPTURE, ServiceStatus.IDLE)
        return False


def recording_command(event):
    '''Run the actual command to record the a/v material.
    '''
    # Prepare command line
    preview_dir = config()['capture']['preview_dir']
    cmd = config()['capture']['command']
    cmd = cmd.replace('{{time}}', str(event.remaining_duration(timestamp())))
    cmd = cmd.replace('{{dir}}', event.directory())
    cmd = cmd.replace('{{name}}', event.name())
    cmd = cmd.replace('{{previewdir}}', preview_dir)

    # Signal configuration
    sigterm_time = config()['capture']['sigterm_time']
    sigkill_time = config()['capture']['sigkill_time']
    sigterm_time = 0 if sigterm_time < 0 else event.end + sigterm_time
    sigkill_time = 0 if sigkill_time < 0 else event.end + sigkill_time

    # Launch capture command
    logger.info(cmd)
    args = shlex.split(cmd)
    DEVNULL = getattr(subprocess, 'DEVNULL', os.open(os.devnull, os.O_RDWR))
    captureproc = subprocess.Popen(args, stdin=DEVNULL)
    hasattr(subprocess, 'DEVNULL') or os.close(DEVNULL)

    # Check process
    while captureproc.poll() is None:
        if sigterm_time and timestamp() > sigterm_time:
            logger.info("Terminating capture process")
            captureproc.terminate()
            sigterm_time = 0  # send only once
        elif sigkill_time and timestamp() > sigkill_time:
            logger.warning("Terminating capture process")
            captureproc.kill()
            sigkill_time = 0  # send only once
        time.sleep(0.1)

    # Remove preview files:
    for preview in config()['capture']['preview']:
        try:
            os.remove(preview.replace('{{previewdir}}', preview_dir))
        except OSError:
            logger.warning('Could not remove preview files')
            logger.warning(traceback.format_exc())

    # Check process for errors
    exitcode = config()['capture']['exit_code']
    if captureproc.poll() > 0 and captureproc.returncode != exitcode:
        raise RuntimeError('Recording failed (%i)' % captureproc.returncode)

    # Return [(flavor,path),…]
    flavors = ensurelist(config()['capture']['flavors'])
    files = ensurelist(config()['capture']['files'])
    files = [f.replace('{{dir}}', event.directory()) for f in files]
    files = [f.replace('{{name}}', event.name()) for f in files]
    return list(zip(flavors, files))


def control_loop():
    '''Main loop of the capture agent, retrieving and checking the schedule as
    well as starting the capture process if necessry.
    '''
    set_service_status(Service.CAPTURE, ServiceStatus.IDLE)
    while not terminate():
        # Get next recording
        event = get_session().query(UpcomingEvent)\
                             .filter(UpcomingEvent.start <= timestamp())\
                             .filter(UpcomingEvent.end > timestamp())\
                             .first()
        if event:
            safe_start_capture(event)
        time.sleep(1.0)
    logger.info('Shutting down capture service')
    set_service_status(Service.CAPTURE, ServiceStatus.STOPPED)


def run():
    '''Start the capture agent.
    '''
    signal.signal(signal.SIGTERM, sigterm_handler)
    configure_service('capture.admin')
    control_loop()
