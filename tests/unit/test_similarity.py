import io

import pytest
from PIL import Image, ImageDraw

from lumen_data.similarity import audit_cross_split
from lumen_data.storage import Store


def page(size=256, changed=False):
    image = Image.new('RGB', (256, 256), '#f4f5f7')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 255, 30), fill='#233347')
    draw.rectangle((8, 42, 63, 245), fill='#9caabc')
    for y in (45, 90, 135, 180):
        draw.rectangle((80, y, 240, y + 24), fill='#ffffff', outline='#334455', width=2)
        draw.rectangle((90, y + 8, 170, y + 11), fill='#223344')
    if changed:
        draw.rectangle((225, 195, 227, 197), fill='#223344')
    return image.resize((size, size))


def add(store, image, scene, split, signature='0000000000000000'):
    out = io.BytesIO()
    image.save(out, format='PNG')
    return (store.put(out.getvalue()), scene, split, signature)


def test_near_duplicate_with_nonidentical_hash_is_confirmed_across_splits(tmp_path):
    store = Store(tmp_path)
    rows = [add(store, page(), 'a', 'train'),
            add(store, page(changed=True), 'b', 'test', f'{sum(1 << i for i in (0, 13, 26, 39)):016x}')]
    report = audit_cross_split(store, iter(rows))
    assert report['status'] == 'complete'
    assert report['confirmed_pairs'] == report['candidate_pairs'] == 1
    assert report['examples'][0]['hamming'] == 4
    assert report['examples'][0]['match'] == 'normalized_content'
    assert not list(tmp_path.glob('.similarity-*'))


def test_same_split_and_repeated_reference_are_not_cross_split_pairs(tmp_path):
    store = Store(tmp_path)
    first = add(store, page(), 'a', 'train')
    report = audit_cross_split(store, iter([first, first, add(store, page(changed=True), 'b', 'train')]))
    assert report['images'] == 2
    assert report['candidate_pairs'] == report['confirmed_pairs'] == 0
    assert report['status'] == 'complete'


def test_normalization_handles_same_page_at_different_resolutions(tmp_path):
    store = Store(tmp_path)
    report = audit_cross_split(store, iter([add(store, page(), 'a', 'train'),
        add(store, page(512, changed=True), 'b', 'val')]))
    assert report['confirmed_pairs'] == 1
    assert report['status'] == 'complete'


def test_unrelated_flat_pages_are_unresolved_not_confirmed(tmp_path):
    store = Store(tmp_path)
    rows = [add(store, Image.new('RGB', (256, 256), color), str(i), split)
            for i, (color, split) in enumerate((('#ffffff', 'train'), ('#fefefe', 'test')))]
    report = audit_cross_split(store, iter(rows))
    assert report['confirmed_pairs'] == 0
    assert report['unresolved_pairs'] == 1
    assert report['status'] == 'incomplete'
    assert report['reason'] == 'low_information'


def test_blank_heavy_pages_with_different_content_do_not_match_by_background(tmp_path):
    store = Store(tmp_path)
    rows = []
    for i, split in enumerate(('train', 'test')):
        image = Image.new('RGB', (256, 256), 'white')
        ImageDraw.Draw(image).rectangle((20 + i * 120, 30, 30 + i * 120, 50), fill='black')
        rows.append(add(store, image, str(i), split))
    report = audit_cross_split(store, iter(rows))
    assert report['candidate_pairs'] == 1
    assert report['confirmed_pairs'] == 0
    assert report['status'] == 'complete'


def test_identical_flat_reference_is_still_confirmed(tmp_path):
    store = Store(tmp_path)
    image = Image.new('RGB', (256, 256), 'white')
    report = audit_cross_split(store, iter([add(store, image, 'a', 'train'), add(store, image, 'b', 'test')]))
    assert report['confirmed_pairs'] == 1
    assert report['unresolved_pairs'] == 0
    assert report['examples'][0]['match'] == 'exact_reference'


def test_five_changed_hash_bits_are_outside_candidate_radius(tmp_path):
    store = Store(tmp_path)
    rows = [add(store, page(), 'a', 'train'),
            add(store, page(changed=True), 'b', 'test', '000000000000001f')]
    report = audit_cross_split(store, iter(rows))
    assert report['candidate_pairs'] == report['confirmed_pairs'] == 0
    assert report['status'] == 'complete'


def test_examples_remain_bounded_when_many_pairs_match(tmp_path):
    store = Store(tmp_path)
    image = page()
    report = audit_cross_split(store, (add(store, image, str(i), f'split-{i}') for i in range(12)))
    assert report['confirmed_pairs'] == 66
    assert len(report['examples']) == 50


def test_candidate_budget_is_explicit_and_does_not_consume_entire_input(tmp_path):
    store = Store(tmp_path)
    consumed = []
    def rows():
        for i in range(20):
            consumed.append(i)
            yield add(store, page(changed=bool(i % 2)), str(i), f'split-{i}')
    report = audit_cross_split(store, rows(), max_candidates=1)
    assert report['status'] == 'incomplete'
    assert report['reason'] == 'candidate_budget_exhausted'
    assert report['indexed_pairs_examined'] == 1
    assert len(consumed) < 20
    assert not list(tmp_path.glob('.similarity-*'))


@pytest.mark.parametrize('budget', [0, -1, True, 1.5])
def test_invalid_candidate_budget_is_rejected(tmp_path, budget):
    with pytest.raises(ValueError, match='candidate budget'):
        audit_cross_split(Store(tmp_path), iter(()), max_candidates=budget)
