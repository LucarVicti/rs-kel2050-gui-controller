#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv_build
source .venv_build/bin/activate
pip install --upgrade pip
pip install -r requirements.txt pyinstaller

pyinstaller --noconfirm --windowed --name rs_kel2050_gui \
  --icon icon.png \
  --add-data "logo.png:." \
  --add-data "icon.png:." \
  rs_kel2050_gui.py

mkdir -p release
(cd dist && zip -r ../release/rs_kel2050_gui_macos.zip rs_kel2050_gui.app)
echo "Built release/rs_kel2050_gui_macos.zip"
