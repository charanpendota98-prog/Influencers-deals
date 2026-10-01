"""WSGI entry point for production Gunicorn workers."""

from dashboard.app import app
from influencer_hub import db

# The development entry point initializes the database in its __main__ block.
# WSGI servers import the module instead, so initialize the schema explicitly.
db.init()
