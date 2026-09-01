#!/usr/bin/env bash
# Render build script — set the service's "Build Command" to: ./build.sh
#
# NOTE: the exact contents of this file were not specified in the brief.
# These are the three standard steps for a Django service on Render. Adjust
# if you need more (e.g. loading a data fixture — see PART 3).
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --no-input

python manage.py migrate
