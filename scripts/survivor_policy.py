"""Shared exclusions for derived discovery outputs; never infer survivor status."""
import json
from pathlib import Path

from victim_policy import ROOT, entity_names, labelled_victim, metadata, name_key, participant_ids, victim_of_sources

__all__ = ['metadata', 'name_key', 'survivor_ids']


def survivor_ids(entities, relationships=(), config=None, key=None):
    """Exclude configured names (including original names and email-header
    participants with exactly that name), labels and victim_of sources."""
    config = Path(config) if config is not None else ROOT / 'config/survivors.json'
    # Missing or malformed exclusions must fail closed, not silently weaken protection.
    names = {name_key(n) for n in json.loads(config.read_text())['names']}
    participants = participant_ids(names, key)
    excluded = {e['id'] for e in entities
                if labelled_victim(e) or e['id'] in participants or any(name_key(n) in names for n in entity_names(e))}
    return excluded | victim_of_sources(relationships)
