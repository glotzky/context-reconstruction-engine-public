import pytest

from cre.db import create_db_engine


def test_engine_hides_bound_parameters() -> None:
    engine = create_db_engine("sqlite://")
    assert engine.hide_parameters is True


def test_empty_database_url_raises() -> None:
    with pytest.raises(ValueError):
        create_db_engine("")
