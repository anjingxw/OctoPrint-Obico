import io
import os
import logging
import subprocess
import time
import sarge
import sys
import flask
from collections import deque
import Queue
from threading import Thread, RLock
import requests
import yaml
from raven import breadcrumbs
import tempfile
import backoff
import json
import socket
import base64
from textwrap import wrap

from .utils import pi_version, ExpoBackoff
from .ws import WebSocketClient
from webcam_capture import capture_jpeg

_logger = logging.getLogger('octoprint.plugins.thespaghettidetective_beta')

POST_PIC_INTERVAL_SECONDS = 10.0
if os.environ.get('DEBUG'):
    POST_PIC_INTERVAL_SECONDS = 5.0

CAM_EXCLUSIVE_USE = os.path.join(tempfile.gettempdir(), '.using_picam')
FFMPEG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'ffmpeg')
JANUS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'janus')

class WebcamStreamer:

    def __init__(self, plugin, sentry):
        self.janus_ws = None
        self.plugin = plugin
        self.sentry = sentry
        self.janus_ws_backoff = ExpoBackoff(120)
        self.picam_streaming = False
        self.last_jpg_post = 0


    @backoff.on_exception(backoff.expo, Exception, max_tries=5)
    def __init_camera__(self):

	import picamera
	self.camera = picamera.PiCamera()
	self.camera.framerate=25
        self.camera.resolution = (640, 480) if self.plugin._settings.effective['webcam'].get('streamRatio', '4:3') == '4:3' else (960, 540)

    def video_pipeline(self):

        self.start_janus()

        mjpeg_thread = Thread(target=self.mjpeg_loop)
        mjpeg_thread.setDaemon(True)
        mjpeg_thread.start()

        # Wait to make sure other plugins that may use pi camera to init first, then yield to them if they are already using pi camera
        time.sleep(10)
        if os.path.exists(CAM_EXCLUSIVE_USE):
            return

        self.picam_streaming = True
        sarge.run('sudo service webcamd stop')

        try:
            self. __init_camera__()

            ffmpeg_cmd = '{} -re -i pipe:0 -c:v copy -bsf dump_extra -an -r 20 -f rtp rtp://0.0.0.0:8004?pkt_size=1300'.format(FFMPEG)
            FNULL = open(os.devnull, 'w')
            ffmpeg_proc = subprocess.Popen(ffmpeg_cmd.split(' '), stdin=subprocess.PIPE, stdout=FNULL, stderr=FNULL)

            self.webcam_server = WebcamServer(self.camera)
            self.webcam_server.start()

            self.camera.start_recording(ffmpeg_proc.stdin, format='h264', quality=23)

        except:
            self.picam_streaming = False
            sarge.run('sudo service webcamd start')   # failed to start picamera. falling back to mjpeg-streamer
            self.sentry.captureException()
            exc_type, exc_obj, exc_tb = sys.exc_info()
            _logger.error(exc_obj)
            return

    @backoff.on_exception(backoff.expo, Exception)
    def mjpeg_loop(self):
        mjpeg_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        while True:
            if not self.picam_streaming or self.last_jpg_post < time.time() - POST_PIC_INTERVAL_SECONDS:
                jpg = None
                try:
                    self.plugin.error_tracker.attempt('webcam')
                    jpg = capture_jpeg(self.plugin._settings)
                except:
                    self.plugin.error_tracker.add_connection_error('webcam')

                if not jpg:
                    continue

                if self.last_jpg_post < time.time() - POST_PIC_INTERVAL_SECONDS:
                    self.plugin.post_jpg(jpg)
                    self.last_jpg_post = time.time()

                if not self.picam_streaming:
                    encoded = base64.b64encode(jpg)
                    msg = '\r\n{}\r\n'.format(len(encoded)) 
                    mjpeg_sock.sendto('\r\n{}\r\n'.format(len(encoded)), ('127.0.0.1', 5008)) #  
                    for chunk in wrap(encoded, 1400):
                        mjpeg_sock.sendto(chunk, ('127.0.0.1', 5008))
                        time.sleep(0.002)

            time.sleep(0.1)


    def pass_to_janus(self, msg):
        if self.janus_ws and self.janus_ws.connected():
            self.janus_ws.send_text(msg)

    def start_janus(self):

        def ensure_janus_config():
	    janus_conf_tmp = os.path.join(JANUS_DIR, 'etc/janus/janus.jcfg.template')
	    janus_conf_path = os.path.join(JANUS_DIR, 'etc/janus/janus.jcfg')
            if os.path.exists(janus_conf_path):
                return

	    with open(janus_conf_tmp, "rt") as fin:
	        with open(janus_conf_path, "wt") as fout:
		    for line in fin:
                        fout.write(line.replace('JANUS_HOME', JANUS_DIR))

        def run_janus():
            env = dict(os.environ)
            env['LD_LIBRARY_PATH'] = JANUS_DIR + '/lib'
            janus_cmd = '{}/bin/janus --stun-server=stun.l.google.com:19302 --configs-folder={}/etc/janus'.format(JANUS_DIR, JANUS_DIR)
            FNULL = open(os.devnull, 'w')
            subprocess.Popen(janus_cmd.split(' '), env=env)# , stdout=FNULL, stderr=FNULL)

        ensure_janus_config()
        janus_thread = Thread(target=run_janus)
        janus_thread.setDaemon(True)
        janus_thread.start()

        self.wait_for_janus()
        self.start_janus_ws_tunnel()

    @backoff.on_exception(backoff.expo, Exception, max_tries=10)
    def wait_for_janus(self):
        time.sleep(1)
        import socket
        socket.socket().connect(('127.0.0.1', 8188))


    def start_janus_ws_tunnel(self):

        def on_close(ws):
            self.janus_ws_backoff.more(Exception('Janus WS connection closed!'))
            self.start_janus_ws_tunnel()

        def on_message(ws, msg):
            self.plugin.ss.send_text(json.dumps(dict(janus=msg)))
            self.janus_ws_backoff.reset()

        self.janus_ws = WebSocketClient('ws://127.0.0.1:8188/', on_ws_msg=on_message, on_ws_close=on_close, subprotocols=['janus-protocol'])
        wst = Thread(target=self.janus_ws.run)
        wst.daemon = True
        wst.start()


class WebcamServer:
    def __init__(self, camera):
        self.camera = camera
        self.img_q = Queue.Queue(maxsize=1)
        self.last_capture = 0
        self._mutex = RLock()

    def capture_forever(self):

        bio = io.BytesIO()
        for foo in self.camera.capture_continuous(bio, format='jpeg', use_video_port=True):
            bio.seek(0)
            chunk = bio.read()
            bio.seek(0)
            bio.truncate()

            with self._mutex:
                last_last_capture = self.last_capture
                self.last_capture = time.time()

            self.img_q.put(chunk)

    def mjpeg_generator(self, boundary):
      try:
        hdr = '--%s\r\nContent-Type: image/jpeg\r\n' % boundary

        prefix = ''
        while True:
            chunk = self.img_q.get()
            msg = prefix + hdr + 'Content-Length: {}\r\n\r\n'.format(len(chunk))
            yield msg.encode('utf-8') + chunk
            prefix = '\r\n'
            time.sleep(0.15) # slow down mjpeg streaming so that it won't use too much cpu or bandwidth
      except GeneratorExit:
         print('closed')

    def get_snapshot(self):
        possible_stale_pics = 3
        while True:
            chunk = self.img_q.get()
            with self._mutex:
                gap = time.time() - self.last_capture
                if gap < 0.1:
                    possible_stale_pics -= 1      # Get a few pics to make sure we are not returning a stale pic, which will throw off Octolapse
                    if possible_stale_pics <= 0:
                        break

        return flask.send_file(io.BytesIO(chunk), mimetype='image/jpeg')

    def get_mjpeg(self):
        boundary='herebedragons'
        return flask.Response(flask.stream_with_context(self.mjpeg_generator(boundary)), mimetype='multipart/x-mixed-replace;boundary=%s' % boundary)

    def run_forever(self):
        webcam_server_app = flask.Flask('webcam_server')

        @webcam_server_app.route('/')
        def webcam():
            action = flask.request.args['action']
            if action == 'snapshot':
                return self.get_snapshot()
            else:
                return self.get_mjpeg()

        webcam_server_app.run(host='0.0.0.0', port=8080, threaded=True)

    def start(self):
        cam_server_thread = Thread(target=self.run_forever)
        cam_server_thread.daemon = True
        cam_server_thread.start()

        capture_thread = Thread(target=self.capture_forever)
        capture_thread.daemon = True
        capture_thread.start()


if __name__ == "__main__":

    streamer = WebcamStreamer()
    streamer.start_video_pipeline(None)
    while True:
        time.sleep(10)