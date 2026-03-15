import hashlib
import json
from pathlib import Path
from typing import Dict, List

import yaml


class ConfigLoader:
    def __init__(self, config_dir: str):
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def list_configs(self) -> List[Dict]:
        configs = []
        for path in sorted(self.config_dir.glob('*.yaml')):
            cfg = self._load_file(path)
            configs.append(cfg)
        for path in sorted(self.config_dir.glob('*.json')):
            cfg = self._load_file(path)
            configs.append(cfg)
        return configs

    def load_config(self, name: str) -> Dict:
        for ext in ('yaml', 'json'):
            candidate = self.config_dir / f"{name}.{ext}"
            if candidate.exists():
                return self._load_file(candidate)
        for path in sorted(self.config_dir.glob('*.yaml')):
            cfg = self._load_file(path)
            if cfg.get('name') == name or cfg.get('config_id') == name:
                return cfg
        for path in sorted(self.config_dir.glob('*.json')):
            cfg = self._load_file(path)
            if cfg.get('name') == name or cfg.get('config_id') == name:
                return cfg
        raise FileNotFoundError(f"Config {name} not found in {self.config_dir}")

    def _load_file(self, path: Path) -> Dict:
        if path.suffix in ['.yaml', '.yml']:
            data = yaml.safe_load(path.read_text())
        else:
            data = json.loads(path.read_text())
        # Compute the content hash before adding helper fields.
        data_hash = self.hash_config(data)
        data['config_id'] = path.stem
        data['__file__'] = str(path)
        data['__hash__'] = data_hash
        return data

    @staticmethod
    def hash_config(data: Dict) -> str:
        clean = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(clean.encode('utf-8')).hexdigest()
