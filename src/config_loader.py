import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional

class ConfigLoader:
    def __init__(self, config_dir: str):
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        # BK2: in-memory cache keyed by stem name; populated on first list/load
        self._cache: Dict[str, Dict] = {}
        self._cache_loaded = False

    def _ensure_cache(self):
        """Load all configs into cache (once per process)."""
        if self._cache_loaded:
            return
        self._cache = {}
        for path in sorted(self.config_dir.glob('*.json')):
            try:
                cfg = self._load_file(path)
                self._cache[path.stem] = cfg
            except (json.JSONDecodeError, OSError) as exc:
                raise ValueError(f"Failed to load config '{path.name}': {exc}") from exc
        self._cache_loaded = True

    def list_configs(self) -> List[Dict]:
        self._ensure_cache()
        return list(self._cache.values())

    def load_config(self, name: str) -> Dict:
        """Load config by stem filename, config name, or config_id. Single scan."""
        self._ensure_cache()
        # Fast path: stem match (e.g. 'j722s' → j722s.json)
        if name in self._cache:
            return self._cache[name]
        # Fallback: match by 'name' or 'config_id' field inside JSON
        for cfg in self._cache.values():
            if cfg.get('name') == name or cfg.get('config_id') == name:
                return cfg
        raise FileNotFoundError(f"Config '{name}' not found in {self.config_dir}")

    def invalidate_cache(self):
        """Force re-scan of config directory on next access."""
        self._cache_loaded = False
        self._cache = {}

    def _load_file(self, path: Path) -> Dict:
        # A4: wrap json.loads to give a clear error with filename
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise json.JSONDecodeError(
                f"Malformed JSON in config file '{path.name}': {exc.msg}",
                exc.doc,
                exc.pos,
            ) from exc
        data_hash = self.hash_config(data)
        data['config_id'] = path.stem
        data['__file__'] = str(path)
        data['__hash__'] = data_hash
        return data

    @staticmethod
    def hash_config(data: Dict) -> str:
        clean = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(clean.encode('utf-8')).hexdigest()
