import pytest

try:
    from aiworkhub.read_efficiency import analyze_read_efficiency
except ImportError:
    from src.aiworkhub.read_efficiency import analyze_read_efficiency


def read(path='a.py', sha='h', offset=0, limit=10, timestamp=None, **extra):
    event = {
        'event_type': 'read',
        'path': path,
        'content_sha256': sha,
        'offset': offset,
        'limit': limit,
        'timestamp': timestamp,
    }
    event.update(extra)
    return event


def test_exact_rereads_require_canonical_path_hash_and_range_match():
    events = [
        read(path='src/./a.py', sha='h1', offset=0, limit=10, timestamp=1),
        read(
            path='src' + chr(92) + 'a.py',
            sha='h1',
            offset=0,
            limit=10,
            timestamp=2,
        ),
        read(path='src/a.py', sha='h2', offset=0, limit=10, timestamp=3),
        read(path='./src/a.py', sha='h1', offset=0, limit=10, timestamp=4),
    ]
    report = analyze_read_efficiency(events)
    assert report.exact_rereads == 2
    assert report.overlap_rereads == 0
    assert report.unknown_repetitions == 1
    assert report.known_repetitions == 2
    assert report.total_reads == 4


def test_overlap_rereads_require_unchanged_hash_and_numeric_overlap():
    events = [
        read(sha='h1', offset=0, limit=10),
        read(sha='h1', offset=5, limit=10),
        read(sha='h1', offset=20, limit=5),
        read(sha='h2', offset=0, limit=10),
        read(sha='h1', offset=0, limit=10),
    ]
    report = analyze_read_efficiency(events)
    assert report.exact_rereads == 1
    assert report.overlap_rereads == 1
    assert report.unknown_repetitions == 1
    assert report.known_repetitions == 2


def test_missing_hash_is_unknown_and_never_false_reread():
    events = [
        read(sha=None, offset=0, limit=10),
        read(sha=None, offset=0, limit=10),
        read(sha='h', offset=0, limit=10),
        read(sha='h', offset=0, limit=10),
    ]
    report = analyze_read_efficiency(events)
    assert report.exact_rereads == 1
    assert report.overlap_rereads == 0
    assert report.unknown_repetitions == 2
    assert report.known_repetitions == 1


def test_bounded_reads_require_numeric_offset_and_positive_limit():
    cases = [
        (0, 10, True),
        (0, 0, False),
        (0, None, False),
        ('0', 10, False),
        (None, 10, False),
        (0, -5, False),
        (1.5, 0.5, True),
    ]
    events = [
        read(
            path='file-%d' % index,
            sha='h%d' % index,
            offset=offset,
            limit=limit,
        )
        for index, (offset, limit, _) in enumerate(cases)
    ]
    report = analyze_read_efficiency(events)
    assert report.bounded_reads == sum(
        1 for _, _, expected in cases if expected
    )
    assert report.unbounded_reads == len(cases) - report.bounded_reads


def test_path_normalization_is_repo_relative_and_deterministic():
    paths = [
        'src/a.py',
        'src/./a.py',
        './src/a.py',
        'src' + chr(92) + 'a.py',
        '/src/a.py',
        'src/../src/a.py',
    ]
    events = [read(path=path, sha='h', offset=0, limit=10) for path in paths]
    report = analyze_read_efficiency(events)
    assert report.total_reads == len(paths)
    assert report.exact_rereads == len(paths) - 1
    assert all(record['path'] == 'src/a.py' for record in report.events)


def test_source_graph_correlation_is_temporal_within_window():
    events = [
        {
            'event_type': 'source_graph',
            'timestamp': 100,
            'source_graph_mode': 'focus',
        },
        read(path='a.py', timestamp=105),
        {
            'event_type': 'source_graph',
            'timestamp': 200,
            'source_graph_mode': 'explore',
        },
        read(path='a.py', timestamp=210),
        read(path='a.py', timestamp=300),
        read(
            path='a.py',
            timestamp=115,
            source_graph_mode='explicit',
            source_graph_timestamp=110,
        ),
    ]
    report = analyze_read_efficiency(events, correlation_window=10)
    assert report.derived_temporal_associations == 2
    assert report.explicit_source_graph_associations == 1
    by_index = {
        record['index']: record
        for record in report.events
        if record['event_type'] == 'read'
    }
    assert by_index[1]['source_graph_mode'] == 'focus'
    assert by_index[1]['temporal_association_only'] is True
    assert by_index[3]['source_graph_mode'] == 'explore'
    assert by_index[4]['source_graph_source'] == 'none'
    assert by_index[5]['source_graph_source'] == 'explicit'
    assert by_index[5]['temporal_association_only'] is False


def test_source_graph_timestamp_is_authoritative_with_generic_fallback():
    events = [
        {
            'source_graph_mode': 'focus',
            'source_graph_timestamp': 100,
        },
        read(path='specific.py', timestamp=105),
        {
            'event_type': 'source_graph',
            'timestamp': 200,
            'source_graph_timestamp': 300,
            'source_graph_mode': 'slice',
        },
        read(path='precedence.py', timestamp=205),
        read(path='precedence.py', timestamp=305),
        {
            'event_type': 'source_graph',
            'timestamp': 400,
            'source_graph_mode': 'context',
        },
        read(path='fallback.py', timestamp=405),
    ]

    report = analyze_read_efficiency(events, correlation_window=10)
    by_index = {
        record['index']: record
        for record in report.events
        if record['event_type'] == 'read'
    }
    assert by_index[1]['source_graph_mode'] == 'focus'
    assert by_index[3]['source_graph_source'] == 'none'
    assert by_index[4]['source_graph_mode'] == 'slice'
    assert by_index[6]['source_graph_mode'] == 'context'


def test_source_graph_tie_break_uses_latest_timestamp_then_original_index():
    events = [
        {
            'event_type': 'source_graph',
            'timestamp': 100,
            'source_graph_mode': 'first',
        },
        read(path='a.py', timestamp=105),
        {
            'event_type': 'source_graph',
            'timestamp': 100,
            'source_graph_mode': 'second',
        },
        read(path='a.py', timestamp=106),
    ]
    report = analyze_read_efficiency(events, correlation_window=10)
    reads = [record for record in report.events if record['event_type'] == 'read']
    assert reads[0]['source_graph_mode'] == 'first'
    assert reads[1]['source_graph_mode'] == 'second'


def test_output_includes_totals_known_unknown_and_is_deterministic():
    events = [
        read(sha='h', offset=0, limit=10),
        read(sha='h', offset=0, limit=10),
        read(sha='h2', offset=0, limit=10),
        read(sha=None, offset=0, limit=10),
    ]
    first = analyze_read_efficiency(events)
    second = analyze_read_efficiency(events)
    assert first.to_dict() == second.to_dict()
    assert first.totals == {'events': 4, 'reads': 4, 'known': 1, 'unknown': 2}
    assert first.known_repetitions + first.unknown_repetitions <= first.total_reads


def test_recommendations_are_pattern_based_and_do_not_claim_savings():
    events = [
        read(sha='h', offset=0, limit=10),
        read(sha=None, offset=0, limit=10),
        read(sha=None, limit=None),
    ]
    report = analyze_read_efficiency(events)
    text = ' '.join(report.recommendations).lower()
    assert 'no token savings' in text
    assert 'no causal savings' in text
    assert any('unknown' in rec.lower() for rec in report.recommendations)
    assert any('bounded' in rec.lower() for rec in report.recommendations)


def test_correlation_window_must_be_nonnegative():
    with pytest.raises(ValueError):
        analyze_read_efficiency([], correlation_window=-1)
