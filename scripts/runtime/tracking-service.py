#!/usr/bin/env python3
"""Tracking engine and local dashboard control API. Runs without Tk or a display."""
import base64
import fcntl
import json
import math
import os
from pathlib import Path
import selectors
import signal
import socket
import time
import uuid

from runtime_config import prepare_engine
from service_protocol import MAX_MESSAGE, PROTOCOL_VERSION, encode, json_safe
from tracking_core import ScreenCalibrationProfile, TOBII_ET5_MARKER_SPACING_MM, TUNING_ATTRS, TUNING_GAIN_BASELINES, save_screen_calibration, tuning_multiplier

TUNING_LIMITS = {
    **{name: (-3., 3.) for name in TUNING_GAIN_BASELINES},
    'opentrack_yaw_curve': (1., 4.), 'opentrack_pitch_curve': (1., 4.),
    'opentrack_output_smoothing': (.01, 1.), 'opentrack_motion_smoothing': (.01, 1.),
    'opentrack_prediction_ms': (0., 120.), 'opentrack_stillness_deadband_deg': (0., .8),
    'opentrack_stillness_velocity_dps': (0., 1000.), 'opentrack_max_angle_step': (0.01, 180.),
    'opentrack_max_translation_step': (.01, 100.), 'opentrack_curve_knee_deg': (5., 90.),
    'blink_hold_s': (0., .6), 'mediapipe_pitch_yaw_comp': (0., 3.),
    'opentrack_max_output_angle': (20., 360.),
}
SNAPSHOT_FIELDS = ('status', 'latest', 'latest_mediapipe', 'pose', 'pose_source', 'translation_source',
    'confidence', 'smoothed_gaze_ratio', 'gaze_affine', 'gaze_offset_x', 'gaze_offset_y',
    'calibration_index', 'screen_profile', 'pose_trail', 'gaze_trail', 'smoothing_enabled',
    'pitch_debug_delta', 'pitch_debug_mapped', 'pitch_debug_raw', 'pitch_debug_status')


class TrackingService:
    def __init__(self, engine):
        self.engine = engine
        self.instance = uuid.uuid4().hex
        self.running = True
        self.calibration_owner = None

    def snapshot(self, client=None, preview=False):
        e = self.engine
        now = time.monotonic()
        result = {key: getattr(e, key) for key in SNAPSHOT_FIELDS}
        result.update(instance=self.instance, pid=os.getpid(), time=now,
            args={key: value for key, value in vars(e.args).items() if key not in ('head_position_calibration', 'native_head_calibration')},
            csv_path=e.csv_path, root_dir=e.root_dir, capture_running=e.proc is not None,
            capture_manual=e.capture_demand.policy.manual, capture_clients=e.capture_demand.policy.client_count(now),
            sample_rate=e.sample_rate(), face_usable=e.latest_mediapipe_usable(),
            calibration_valid_samples=sum(s.gaze_valid == 1 and math.isfinite(s.gaze_x) and math.isfinite(s.gaze_y) for s in e.samples))
        if self.calibration_owner not in (None,client):result['calibration_index']=None
        result['window_geometry']='1680x1050'
        try:
            window=json.loads(e.args.window_state_file.read_text())
            result['window_geometry']=f"{int(window['width'])}x{int(window['height'])}{int(window['x']):+d}{int(window['y']):+d}"
        except (OSError,ValueError,TypeError,KeyError):pass
        bridge=e.opentrack_bridge
        result['output'] = None if bridge is None else {key: getattr(bridge,key) for key in
            ('last_packet','last_pose_source','last_send_monotonic','sent','last_error')}
        bridge=e.mediapipe_bridge
        result['mediapipe'] = None if bridge is None else {key: getattr(bridge,key) for key in
            ('frames_seen','frames_sent','frames_present','last_frame_monotonic','last_error')}
        if preview:
            image = None
            if bridge is not None:
                with bridge.lock:
                    frame = bridge.preview
                if frame is not None:
                    pixels, landmarks = frame
                    image = dict(pgm=base64.b64encode(pixels).decode('ascii'), landmarks=landmarks)
            result['preview'] = image
        result['hq'] = e.hq_worker.snapshot() if e.hq_worker is not None else None
        return result

    def disconnect(self, client):
        if self.calibration_owner == client:
            self.engine.clear_gaze_runtime_state()
            self.calibration_owner = None
            self.engine.status = 'gaze calibration cancelled: dashboard disconnected'

    def command(self, client, name, values):
        e=self.engine
        if name == 'set_capture_manual':
            value=values['value']
            if value is not None and not isinstance(value,bool):raise ValueError('invalid capture mode')
            e.set_capture_manual(value)
        elif name == 'set_harness':
            if values['harness'] not in ('tobii','trackir'):raise ValueError('invalid output harness')
            e.set_harness(values['harness'])
        elif name == 'adjust_gain':
            if values['axis'] not in ('yaw','pitch','roll'):raise ValueError('invalid gain axis')
            delta=float(values['delta'])
            if not math.isfinite(delta) or abs(delta)>1:raise ValueError('invalid gain increment')
            e.adjust_gain(values['axis'],delta)
        elif name == 'set_tuning':
            updates=values['updates']
            if not isinstance(updates,dict):raise ValueError('expected tuning values')
            checked={}
            for key,value in updates.items():
                if key not in TUNING_LIMITS or isinstance(value,bool):raise ValueError('unsupported tuning value')
                value=float(value)
                scaled=tuning_multiplier(key,value)
                lo,hi=TUNING_LIMITS[key]
                if not math.isfinite(scaled) or not lo-1e-9<=scaled<=hi+1e-9:raise ValueError(f'{key} outside supported range')
                checked[key]=value
            for key,value in checked.items():setattr(e.args,key,value)
            e.save_tuning()
        elif name == 'save_screen':
            monitor=values['monitor']
            width,height=int(monitor['width']),int(monitor['height'])
            left,right=float(values['left']),float(values['right'])
            if not 1<=width<=32768 or not 1<=height<=32768:raise ValueError('invalid display size')
            if not all(math.isfinite(v) for v in (left,right)) or not 0<=left<right<=width:raise ValueError('invalid marker positions')
            spacing=right-left
            if spacing<80 or spacing>width-40 or left<20 or right>width-20:
                raise ValueError('marker positions outside supported display region')
            profile=ScreenCalibrationProfile(1,time.time(),str(monitor['name'])[:256],
                int(monitor['x']),int(monitor['y']),width,height,TOBII_ET5_MARKER_SPACING_MM,
                spacing,spacing/TOBII_ET5_MARKER_SPACING_MM,TOBII_ET5_MARKER_SPACING_MM/spacing,
                (left+right)*.5,float(height))
            if not save_screen_calibration(e.args.screen_calibration_file,profile):raise OSError('could not save screen calibration')
            e.screen_profile=profile
            e.status='screen calibrated'
        elif name == 'save_window':
            geometry={key:int(values[key]) for key in ('width','height','x','y')}
            if not 200<=geometry['width']<=32768 or not 200<=geometry['height']<=32768:raise ValueError('invalid window size')
            path=e.args.window_state_file
            path.parent.mkdir(parents=True,exist_ok=True)
            temporary=path.with_suffix(path.suffix+'.tmp')
            temporary.write_text(json.dumps(geometry)+'\n');temporary.replace(path)
        elif name == 'start_gaze_calibration':
            if e.proc is None:raise ValueError('Start capture before gaze calibration')
            if self.calibration_owner not in (None,client):raise ValueError('another dashboard is calibrating gaze')
            self.calibration_owner=client;e.start_gaze_calibration()
        elif name == 'capture_calibration_point':
            if self.calibration_owner != client:raise ValueError('this dashboard does not own gaze calibration')
            e.capture_calibration_point()
            if e.calibration_index is None:self.calibration_owner=None
        elif name == 'cancel_gaze_calibration':
            if self.calibration_owner not in (None,client):raise ValueError('another dashboard is calibrating gaze')
            e.clear_gaze_runtime_state();self.calibration_owner=None
        elif name in ('recenter','reset_baseline','reset_tuning','reset_gaze_calibration','center_gaze','toggle_smoothing','dump_pitch_debug'):
            getattr(e,name)()
        else:
            raise ValueError('unsupported service command')
        return {'status':e.status}

    def dispatch(self, client, request):
        if not isinstance(request,dict) or request.get('version')!=PROTOCOL_VERSION:
            raise ValueError('unsupported service protocol')
        if request.get('op')=='snapshot':return self.snapshot(client, request.get('preview') is True)
        if request.get('op')=='ping':return {'pid':os.getpid(),'instance':self.instance}
        if request.get('op')=='command':return self.command(client,request.get('name'),request.get('values',{}))
        raise ValueError('unsupported service operation')

    def run(self, path):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        lock=path.with_suffix('.lock').open('a')
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close();raise RuntimeError('tracking service already owns this control socket')
        listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        selector=selectors.DefaultSelector();clients={}
        try:
            path.unlink(missing_ok=True)
            listener.bind(str(path));path.chmod(0o600);listener.listen(16);listener.setblocking(False)
            selector.register(listener,selectors.EVENT_READ)
            print(f'tracking_service=ready socket={path} pid={os.getpid()}',flush=True)
            next_tick=time.monotonic()
            while self.running:
                now=time.monotonic()
                if now>=next_tick:
                    self.engine.tick()
                    next_tick=time.monotonic()+max(.005,self.engine.args.interval_ms/1000.)
                for key,mask in selector.select(timeout=max(0.,min(.05,next_tick-time.monotonic()))):
                    if key.fileobj is listener:
                        conn,_=listener.accept();conn.setblocking(False)
                        if len(clients)>=16:conn.close();continue
                        clients[conn]={'input':bytearray(),'output':bytearray()}
                        selector.register(conn,selectors.EVENT_READ)
                        continue
                    conn=key.fileobj;state=clients[conn]
                    try:
                        if mask & selectors.EVENT_READ:
                            data=conn.recv(65536)
                            if not data:raise ConnectionError('dashboard disconnected')
                            state['input'].extend(data)
                            if len(state['input'])>MAX_MESSAGE:raise ConnectionError('request too large')
                            for _ in range(16):
                                if b'\n' not in state['input']:break
                                line,_,rest=state['input'].partition(b'\n');state['input']=bytearray(rest)
                                try:
                                    result=self.dispatch(conn,json.loads(line))
                                    reply=dict(version=PROTOCOL_VERSION,ok=True,result=result)
                                except (ValueError,KeyError,TypeError,OSError,OverflowError) as exc:
                                    reply=dict(version=PROTOCOL_VERSION,ok=False,error=str(exc))
                                state['output'].extend(encode(reply))
                            if len(state['output'])>2*MAX_MESSAGE:raise ConnectionError('dashboard too slow')
                        if mask & selectors.EVENT_WRITE and state['output']:
                            sent=conn.send(state['output']);del state['output'][:sent]
                        selector.modify(conn,selectors.EVENT_READ|(selectors.EVENT_WRITE if state['output'] else 0))
                    except (OSError,ConnectionError):
                        self.disconnect(conn);selector.unregister(conn);conn.close();clients.pop(conn,None)
        finally:
            for conn in clients:conn.close()
            selector.close();listener.close();path.unlink(missing_ok=True);lock.close()


def main():
    engine=prepare_engine()
    service=TrackingService(engine)
    signal.signal(signal.SIGTERM,lambda *_:setattr(service,'running',False))
    signal.signal(signal.SIGINT,lambda *_:setattr(service,'running',False))
    try:
        service.run(engine.args.control_socket)
    finally:
        engine.stop_capture();engine.capture_demand.close()
        if engine.opentrack_bridge is not None:engine.opentrack_bridge.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
