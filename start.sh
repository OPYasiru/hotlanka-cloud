#!/bin/bash
pkill -9 Xvfb x11vnc websockify openbox chrome python3 2>/dev/null || true
sleep 1

export DISPLAY=:1
Xvfb :1 -screen 0 1280x720x24 &
sleep 1

openbox &
sleep 1

x11vnc -display :1 -rfbport 5900 -nopw -forever -shared -bg
websockify -D --web=/usr/share/novnc/ 6080 localhost:5900

google-chrome --no-sandbox --disable-dev-shm-usage --window-position=0,0 --window-size=1280,720 &

nohup python3 app.py > /tmp/hotlanka.log 2>&1 &
