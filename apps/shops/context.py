def store_context(request):
    return {"store": getattr(request, "store", None)}

