import collections
import time

_logs = collections.deque(maxlen=300)

def log(msg):
    _logs.appendleft({"t": time.strftime("%H:%M:%S"), "msg": msg})
