"""Stable job task names shared by API and worker bookkeeping.

Keep these constants dependency-free: worker startup recovery runs in both the
full worker image and the deliberately smaller paper worker image.
"""

MEDIA_AUTH_LEASE_TASK = "_media_auth"
