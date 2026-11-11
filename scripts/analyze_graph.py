"""Build a provenance-preserving multigraph; summarize topology, not culpability."""
import csv
import json
from collections import Counter
from pathlib import Path

import networkx as nx
from build_contacts import main as contacts_main

ROOT = Path(__file__).resolve().parents[1]
RAW, OUT = ROOT / 'data/raw', ROOT / 'output'


def main():
    contacts_main()
    entities = json.loads((OUT / 'graph_entities.json').read_text())
    relationships = json.loads((OUT / 'graph_relationships.json').read_text())
    graph = nx.MultiDiGraph()
    ids = {e['id'] for e in entities}
    assert len(ids) == len(entities), 'Duplicate entity IDs'
    assert len({r['id'] for r in relationships}) == len(relationships), 'Duplicate relationship IDs'
    for entity in entities:
        graph.add_node(entity['id'], **entity)
    for rel in relationships:
        source, target = rel['source_entity_id'], rel['target_entity_id']
        assert source in ids and target in ids, f'Dangling relationship {rel["id"]}'
        graph.add_edge(source, target, key=rel['id'], **rel)
    # Collapse directions/types ONLY for explicitly unweighted topology summaries.
    topology = nx.Graph(graph)
    topology.remove_edges_from(nx.selfloop_edges(topology))
    components = sorted(nx.connected_components(topology), key=len, reverse=True)
    summary = {
        'entities': len(entities), 'relationships': len(relationships),
        'normalization': 'Exact NFC/casefold/whitespace names within entity type; see entity_merges.json. Original records retained. No alias/fuzzy resolution.',
        'entity_types': dict(Counter(e['entity_type'] for e in entities)),
        'relationship_types': dict(Counter(r['relationship_type'] for r in relationships)),
        'connected_components_undirected': len(components),
        'component_sizes': [len(c) for c in components],
        'isolates': len(list(nx.isolates(topology))),
        'warning': 'Upstream labels/assertions are not independently verified. Edges and centrality do not establish wrongdoing. Weights have type-dependent meanings, not confidence.',
    }
    OUT.mkdir(exist_ok=True)
    (OUT / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    # JSON preserves nulls, metadata strings, parallel edges, dates and upstream IDs.
    (OUT / 'graph.json').write_text(json.dumps(nx.node_link_data(graph), indent=2) + '\n')
    with (OUT / 'entity_connectivity.csv').open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['entity_id', 'name', 'entity_type', 'unique_neighbors_unweighted', 'relationship_records'])
        for entity in sorted(entities, key=lambda e: (-topology.degree(e['id']), str(e['id']))):
            i = entity['id']
            # MultiDiGraph.degree counts a self-loop twice; report records once.
            writer.writerow([i, entity['name'], entity['entity_type'], topology.degree(i),
                             graph.degree(i) - graph.number_of_edges(i, i)])
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
