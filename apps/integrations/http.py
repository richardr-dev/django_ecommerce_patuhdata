import requests


class ProviderError(RuntimeError):
    pass


DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "NiagaKit-Commerce/0.1",
    "Cache-Control": "no-cache",
}


def _request(url, *, method, headers, timeout, **kwargs):
    last_error = None
    for _attempt in range(2):
        try:
            response = requests.request(method, url, headers={**DEFAULT_HEADERS, **(headers or {})},
                timeout=(10, timeout), **kwargs)
            if not response.ok:
                raise ProviderError(f"Provider returned HTTP {response.status_code}: {response.text[:500]}")
            return response.json()
        except ProviderError:
            raise
        except (requests.ConnectionError, requests.Timeout, requests.JSONDecodeError) as error:
            last_error = error
    raise ProviderError("Provider is temporarily unavailable after retry.") from last_error


def request_json(url, *, method="GET", headers=None, payload=None, encoded_payload=None, timeout=20):
    kwargs = {"data": encoded_payload} if encoded_payload is not None else {"json": payload}
    return _request(url, method=method, headers={"Content-Type": "application/json", **(headers or {})},
        timeout=timeout, **kwargs)


def request_form_json(url, payload, *, headers=None, timeout=20):
    return _request(url, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})},
        timeout=timeout, data=payload)
