class HtmxMiddleware:
    """Flag requests made by HTMX that expect an HTML fragment instead of a full page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.htmx_partial = (
            request.headers.get("HX-Request") == "true"
            and request.headers.get("HX-Boosted") != "true"
        )
        response = self.get_response(request)
        response.headers.setdefault("Vary", "HX-Request")
        return response
