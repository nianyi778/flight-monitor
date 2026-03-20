#!/usr/bin/with-contenv bash
set -euo pipefail
mkdir -p /config/.config/openbox
cat >/config/.config/openbox/autostart <<'EOF'
#!/bin/bash
rm -f /config/chrome-profile/SingletonCookie /config/chrome-profile/SingletonLock /config/chrome-profile/SingletonSocket
wrapped-chromium ${CHROME_CLI}
EOF
chmod +x /config/.config/openbox/autostart
