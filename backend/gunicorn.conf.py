def post_fork(server, worker):
    # Backend workers get recycled every ~400 requests (see entrypoint.sh),
    # so this can't be a one-time startup cost -- it needs to happen after
    # every fork, or the first synchronous reverse-geocode call on each
    # fresh worker (MAPS_ON_UPLOAD=true) would pay the ~1s offline K-D tree
    # build inline on a real upload request.
    import maps_utils
    maps_utils.prewarm_offline_geocoder()
