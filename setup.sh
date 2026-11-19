#!/bin/bash
sudo apt-get update -qq
sudo apt-get install -y -qq wget ffmpeg python3-pip libgl1

# Install Google Chrome
wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt-get install -y -qq /tmp/chrome.deb
rm -f /tmp/chrome.deb

# Install Python Libraries (Flask, Telethon, Fast Encryption, OpenCV, Pillow)
pip3 install -q flask telethon cryptg opencv-python-headless numpy pillow

# Create Working Directories
mkdir -p ~/Downloads ~/Edited ~/Edited/preview ~/Edited/completed ~/Edited/temp ~/Desktop

# Create Desktop Shortcut for Chrome + VDH
cat << 'EOF' > ~/Desktop/Chrome_VDH.desktop
[Desktop Entry]
Name=Chrome (VDH)
Exec=google-chrome --no-sandbox https://chromewebstore.google.com/detail/video-downloadhelper/lmjnegcaeklhafolokijcfjliaokphfk
Type=Application
Terminal=false
Icon=google-chrome
EOF
chmod +x ~/Desktop/Chrome_VDH.desktop
