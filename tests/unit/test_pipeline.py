import io

import numpy as np
import pytest
from PIL import Image

from lumen_data.pipeline import build_record, image_artifacts
from lumen_data.pipeline import recipe, ARCHETYPES
from lumen_data.storage import Store


def png(array):
    stream = io.BytesIO()
    Image.fromarray(array).save(stream, format='PNG')
    return stream.getvalue()


def test_exact_change_mask_and_case_flags(tmp_path):
    before = np.full((100, 200, 3), 255, np.uint8)
    after = before.copy()
    after[10:20, 30:50] = 0
    store = Store(tmp_path)
    assets, stats = image_artifacts(store, png(before), png(after), [200, 100])
    mask = np.array(Image.open(io.BytesIO(store.read(assets['raw_change_mask']))))
    assert np.count_nonzero(mask) == 200
    assert stats['changed_pixels'] == 200
    assert assets['reference'] != assets['current']


def test_wrong_dimensions_and_no_local_effect_are_rejected(tmp_path):
    white = png(np.full((100, 200, 3), 255, np.uint8))
    with pytest.raises(ValueError, match='dimensions'):
        image_artifacts(Store(tmp_path), white, white, [200, 200])
    with pytest.raises(ValueError):
        build_record({}, {'status': 'unverified'}, {}, {})


@pytest.mark.parametrize('variants', [1, 2, 7, 10, 16])
def test_scene_family_does_not_determine_positive_or_negative(variants):
    classes = {family: set() for family in ARCHETYPES}
    for index in range(4000):
        payload = recipe(index, {'variants_per_scene': variants, 'seed': 123}, 'v1')
        classes[payload['scene']['archetype']].add(payload['operator'].startswith(('clean', 'benign')))
    assert all(values == {True, False} for values in classes.values())
