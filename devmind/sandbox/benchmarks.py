"""Three deterministic, local multi-file engineering benchmarks."""
from __future__ import annotations

import asyncio
from pathlib import Path

from devmind.sandbox.workspace import _git


CASES: dict[str, dict[str, str]] = {
    "async-rate-limiter": {
        "limiter.py": '''import asyncio\n\nclass RateLimiter:\n    def __init__(self, limit: int):\n        self.limit = limit\n        self.used = 0\n\n    async def acquire(self) -> bool:\n        if self.used >= self.limit:\n            return False\n        await asyncio.sleep(0)  # yields between check and update\n        self.used += 1\n        return True\n''',
        "service.py": '''from limiter import RateLimiter\n\nasync def serve(requests: int, limit: int) -> int:\n    limiter = RateLimiter(limit)\n    import asyncio\n    return sum(await asyncio.gather(*(limiter.acquire() for _ in range(requests))))\n''',
        "tests/test_limiter.py": '''import asyncio\nfrom limiter import RateLimiter\nfrom service import serve\n\ndef test_concurrent_acquires_never_exceed_limit():\n    assert asyncio.run(serve(100, 4)) == 4\n\ndef test_exhausted_limiter_rejects():\n    async def run():\n        r = RateLimiter(1)\n        assert await r.acquire()\n        assert not await r.acquire()\n    asyncio.run(run())\n''',
    },
    "jwt-algorithm-confusion": {
        "auth/token.py": '''import base64, json\n\ndef _decode(value: str) -> dict:\n    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))\n\ndef verify(token: str, secret: str) -> dict:\n    header, payload, signature = token.split(".")\n    metadata = _decode(header)\n    if metadata.get("alg") == "none":\n        return _decode(payload)\n    import hmac, hashlib\n    expected = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()\n    actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))\n    if not hmac.compare_digest(expected, actual):\n        raise ValueError("invalid signature")\n    return _decode(payload)\n''',
        "auth/service.py": '''from auth.token import verify\n\ndef current_user(token: str, secret: str) -> str:\n    claims = verify(token, secret)\n    if "sub" not in claims:\n        raise ValueError("missing subject")\n    return claims["sub"]\n''',
        "tests/test_auth.py": '''import base64, json, pytest\nfrom auth.service import current_user\n\ndef enc(data):\n    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")\n\ndef test_unsigned_token_is_rejected():\n    forged = f"{enc({'alg': 'none'})}.{enc({'sub': 'admin'})}."\n    with pytest.raises(ValueError):\n        current_user(forged, "server-secret")\n\ndef test_signed_token_is_accepted():\n    import hashlib, hmac\n    h, p = enc({'alg': 'HS256'}), enc({'sub': 'alice'})\n    sig = base64.urlsafe_b64encode(hmac.new(b"server-secret", f"{h}.{p}".encode(), hashlib.sha256).digest()).decode().rstrip("=")\n    assert current_user(f"{h}.{p}.{sig}", "server-secret") == "alice"\n''',
    },
    "atomic-inventory-reservation": {
        "inventory.py": '''import asyncio\n\nclass Inventory:\n    def __init__(self, count: int):\n        self.count = count\n        self.reserved = 0\n\n    async def reserve(self) -> bool:\n        if self.reserved >= self.count:\n            return False\n        await asyncio.sleep(0)\n        self.reserved += 1\n        return True\n''',
        "checkout.py": '''from inventory import Inventory\nimport asyncio\n\nasync def checkout(inventory: Inventory, orders: int) -> int:\n    return sum(await asyncio.gather(*(inventory.reserve() for _ in range(orders))))\n''',
        "tests/test_inventory.py": '''import asyncio\nfrom inventory import Inventory\nfrom checkout import checkout\n\ndef test_checkout_never_oversells():\n    stock = Inventory(3)\n    assert asyncio.run(checkout(stock, 40)) == 3\n    assert stock.reserved == 3\n\ndef test_empty_stock():\n    assert asyncio.run(checkout(Inventory(0), 3)) == 0\n''',
    },
}


async def create_benchmark(name: str, destination: str | Path) -> Path:
    """Materialize a real local Git repository and runnable pytest suite."""
    if name not in CASES:
        raise ValueError(f"Unknown benchmark {name!r}; choose {', '.join(CASES)}")
    root = Path(destination).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError(f"Benchmark destination must be empty: {root}")
    for relative, source in CASES[name].items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    await _git("init", "-q", cwd=root)
    await _git("config", "--local", "user.name", "DevMind", cwd=root)
    await _git("config", "--local", "user.email", "bot@devmind.local", cwd=root)
    await _git("add", "-A", cwd=root)
    await _git("commit", "-qm", f"Benchmark: {name}", cwd=root)
    return root


def available_benchmarks() -> list[str]:
    return sorted(CASES)
