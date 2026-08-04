'''Pure read-efficiency analysis kernel.

Consumes already-normalized event mappings.  This module never touches the
filesystem and never performs repository or log scanning.
'''

from __future__ import annotations

import math
import posixpath
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

SOURCE_GRAPH_EVENT_TYPE = 'source_graph'


def _is_number(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value) and not math.isnan(value)
    return False


def canonical_path(path: Any) -> Optional[str]:
    '''Return a deterministic repo-relative canonical path.'''
    if path is None:
        return None
    text = str(path).replace(chr(92), '/')
    normalized = posixpath.normpath(text).lstrip('/')
    return normalized or '.'


def _bounded_range(offset: Any, limit: Any) -> Optional[Tuple[float, float]]:
    if not _is_number(offset) or not _is_number(limit):
        return None
    if limit <= 0:
        return None
    return offset, limit


def _same_range(
    left: Optional[Tuple[float, float]],
    right: Optional[Tuple[float, float]],
) -> bool:
    return left is not None and right is not None and left == right


def _ranges_overlap(
    left: Optional[Tuple[float, float]],
    right: Optional[Tuple[float, float]],
) -> bool:
    if left is None or right is None:
        return False
    return left[0] < right[0] + right[1] and right[0] < left[0] + left[1]


def _event_type(event: Mapping[str, Any]) -> str:
    value = event.get('event_type')
    if value is None:
        return ''
    return str(value).strip().lower()


def _event_timestamp(event: Mapping[str, Any]) -> Optional[float]:
    timestamp = event.get('timestamp')
    return timestamp if _is_number(timestamp) else None


def _source_graph_event_timestamp(event: Mapping[str, Any]) -> Optional[float]:
    """Return Source Graph time, preferring its domain-specific timestamp."""
    timestamp = event.get('source_graph_timestamp')
    if _is_number(timestamp):
        return timestamp
    return _event_timestamp(event)


def _is_source_graph_event(event: Mapping[str, Any]) -> bool:
    if _event_type(event) == SOURCE_GRAPH_EVENT_TYPE:
        return True
    if event.get('path') is None:
        if (
            event.get('source_graph_mode') is not None
            and event.get('source_graph_timestamp') is not None
        ):
            return True
    return False


def _is_read_event(event: Mapping[str, Any]) -> bool:
    if _is_source_graph_event(event):
        return False
    return event.get('path') is not None


@dataclass
class ReadEfficiencyReport:
    total_events: int
    total_reads: int
    known_repetitions: int
    unknown_repetitions: int
    exact_rereads: int
    overlap_rereads: int
    bounded_reads: int
    unbounded_reads: int
    explicit_source_graph_associations: int
    derived_temporal_associations: int
    recommendations: List[str] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def totals(self) -> Dict[str, int]:
        return {
            'events': self.total_events,
            'reads': self.total_reads,
            'known': self.known_repetitions,
            'unknown': self.unknown_repetitions,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            'total_events': self.total_events,
            'total_reads': self.total_reads,
            'known_repetitions': self.known_repetitions,
            'unknown_repetitions': self.unknown_repetitions,
            'exact_rereads': self.exact_rereads,
            'overlap_rereads': self.overlap_rereads,
            'bounded_reads': self.bounded_reads,
            'unbounded_reads': self.unbounded_reads,
            'explicit_source_graph_associations': self.explicit_source_graph_associations,
            'derived_temporal_associations': self.derived_temporal_associations,
            'totals': self.totals,
            'recommendations': list(self.recommendations),
            'events': [dict(record) for record in self.events],
        }


def _build_recommendations(counts: Mapping[str, int]) -> List[str]:
    recommendations: List[str] = []
    if counts['total_reads'] == 0:
        recommendations.append('No reads observed; no repetition analysis is possible.')
    if counts['unknown_repetitions']:
        recommendations.append(
            'Attach consistent nonempty content hashes to reduce unknown repetitions.'
        )
    if counts['unbounded_reads']:
        recommendations.append(
            'Supply numeric offsets and positive limits to enable bounded range analysis.'
        )
    if counts['exact_rereads']:
        recommendations.append(
            'Exact rereads detected; prefer reusing identical bounded ranges.'
        )
    if counts['overlap_rereads']:
        recommendations.append(
            'Overlapping rereads are present; align repeated read ranges to reduce redundant byte traffic.'
        )
    recommendations.append('No token savings are claimed; no causal savings are claimed.')
    return recommendations


def analyze_read_efficiency(
    events: Iterable[Mapping[str, Any]],
    correlation_window: float = 0,
    **kwargs: Any,
) -> ReadEfficiencyReport:
    if kwargs.get('window') is not None:
        correlation_window = kwargs['window']
    if kwargs.get('source_graph_window') is not None:
        correlation_window = kwargs['source_graph_window']
    if not _is_number(correlation_window) or correlation_window < 0:
        raise ValueError('correlation_window must be a nonnegative number')

    event_list = list(events)
    ranges_by_path_hash: Dict[str, Dict[str, List[Tuple[float, float]]]] = {}
    hashes_by_path: Dict[str, Set[str]] = {}
    seen_paths: Set[str] = set()
    source_graph_events: List[Tuple[float, int, Any]] = []
    report_events: List[Dict[str, Any]] = []

    counts: Dict[str, int] = {
        'total_reads': 0,
        'exact_rereads': 0,
        'overlap_rereads': 0,
        'unknown_repetitions': 0,
        'bounded_reads': 0,
        'unbounded_reads': 0,
        'explicit_source_graph_associations': 0,
        'derived_temporal_associations': 0,
    }

    for original_index, event in enumerate(event_list):
        if _is_source_graph_event(event):
            timestamp = _source_graph_event_timestamp(event)
            if timestamp is not None:
                source_graph_events.append(
                    (timestamp, original_index, event.get('source_graph_mode'))
                )
            report_events.append(
                {
                    'index': original_index,
                    'event_type': _event_type(event) or SOURCE_GRAPH_EVENT_TYPE,
                    'path': canonical_path(event.get('path')),
                    'content_sha256': None,
                    'offset': event.get('offset'),
                    'limit': event.get('limit'),
                    'bounded': False,
                    'repetition': 'none',
                    'source_graph_mode': event.get('source_graph_mode'),
                    'source_graph_timestamp': timestamp,
                    'source_graph_source': 'event',
                    'classification_source': 'none',
                    'temporal_association_only': False,
                }
            )
            continue

        if not _is_read_event(event):
            continue

        path = canonical_path(event.get('path'))
        if path is None:
            continue

        offset = event.get('offset')
        limit = event.get('limit')
        bounded_range = _bounded_range(offset, limit)
        bounded = bounded_range is not None

        raw_hash = event.get('content_sha256')
        content_hash: Optional[str] = None
        if raw_hash is not None:
            hash_text = str(raw_hash)
            if hash_text:
                content_hash = hash_text

        repetition = 'none'
        if path in seen_paths:
            known_hashes = hashes_by_path.setdefault(path, set())
            if content_hash is None or content_hash not in known_hashes:
                repetition = 'unknown'
            else:
                ranges = ranges_by_path_hash.setdefault(path, {}).get(
                    content_hash, []
                )
                if any(_same_range(bounded_range, prior) for prior in ranges):
                    repetition = 'exact'
                elif any(
                    bounded_range is not None
                    and _ranges_overlap(bounded_range, prior)
                    for prior in ranges
                ):
                    repetition = 'overlap'
        else:
            seen_paths.add(path)
            hashes_by_path.setdefault(path, set())

        if content_hash is not None:
            hashes_by_path.setdefault(path, set()).add(content_hash)
            if bounded_range is not None:
                ranges_by_path_hash.setdefault(path, {}).setdefault(
                    content_hash, []
                ).append(bounded_range)

        counts['total_reads'] += 1
        if bounded:
            counts['bounded_reads'] += 1
        else:
            counts['unbounded_reads'] += 1
        if repetition == 'exact':
            counts['exact_rereads'] += 1
        elif repetition == 'overlap':
            counts['overlap_rereads'] += 1
        elif repetition == 'unknown':
            counts['unknown_repetitions'] += 1

        source_graph_mode: Any = None
        source_graph_timestamp: Any = None
        source_graph_source = 'none'
        temporal_association_only = False
        explicit_mode = event.get('source_graph_mode')
        explicit_timestamp = event.get('source_graph_timestamp')
        if (
            explicit_mode is not None
            and explicit_timestamp is not None
            and str(explicit_mode) != ''
        ):
            source_graph_mode = str(explicit_mode)
            source_graph_timestamp = explicit_timestamp
            source_graph_source = 'explicit'
            temporal_association_only = False
            counts['explicit_source_graph_associations'] += 1
        else:
            event_timestamp = _event_timestamp(event)
            best: Optional[Tuple[float, int, Any]] = None
            if event_timestamp is not None:
                for cand_ts, cand_index, cand_mode in source_graph_events:
                    if cand_ts > event_timestamp:
                        continue
                    if event_timestamp - cand_ts > correlation_window:
                        continue
                    if best is None or (cand_ts, cand_index) > (best[0], best[1]):
                        best = (cand_ts, cand_index, cand_mode)
            if best is not None:
                source_graph_mode = (
                    str(best[2]) if best[2] is not None else None
                )
                source_graph_timestamp = best[0]
                source_graph_source = 'derived'
                temporal_association_only = True
                counts['derived_temporal_associations'] += 1

        report_events.append(
            {
                'index': original_index,
                'event_type': _event_type(event) or 'read',
                'path': path,
                'content_sha256': content_hash,
                'offset': offset,
                'limit': limit,
                'bounded': bounded,
                'repetition': repetition,
                'source_graph_mode': source_graph_mode,
                'source_graph_timestamp': source_graph_timestamp,
                'source_graph_source': source_graph_source,
                'classification_source': (
                    'explicit'
                    if source_graph_source == 'explicit'
                    else 'analyzer-derived'
                    if source_graph_source == 'derived'
                    else 'none'
                ),
                'temporal_association_only': temporal_association_only,
            }
        )

    known_repetitions = counts['exact_rereads'] + counts['overlap_rereads']
    return ReadEfficiencyReport(
        total_events=len(event_list),
        total_reads=counts['total_reads'],
        known_repetitions=known_repetitions,
        unknown_repetitions=counts['unknown_repetitions'],
        exact_rereads=counts['exact_rereads'],
        overlap_rereads=counts['overlap_rereads'],
        bounded_reads=counts['bounded_reads'],
        unbounded_reads=counts['unbounded_reads'],
        explicit_source_graph_associations=counts['explicit_source_graph_associations'],
        derived_temporal_associations=counts['derived_temporal_associations'],
        recommendations=_build_recommendations(counts),
        events=report_events,
    )
