from __future__ import annotations

import pytest
from pydantic import ValidationError

from corporate_kb.confluence.models import SourceConfig


def source_config(**values):
    return SourceConfig.model_validate(
        {
            "name": "Documentation",
            "index_id": "docs",
            "base_url": "https://confluence.example.com",
            "root_page_url": "123",
            **values,
        }
    )


@pytest.mark.parametrize(
    "deployment,base_url",
    [
        ("cloud", "https://confluence.example.com"),
        ("auto", "https://team.atlassian.net/wiki"),
        ("auto", "https://TEAM.ATLASSIAN.NET:443/wiki"),
    ],
)
@pytest.mark.parametrize("username", [None, "", " \t "])
def test_cloud_requires_account_email_with_token(deployment, base_url, username):
    with pytest.raises(ValidationError, match="Cloud requires an Atlassian account email"):
        source_config(deployment=deployment, base_url=base_url, username=username)


@pytest.mark.parametrize("username", ["account@example.com", "service-account"])
def test_cloud_trims_username_without_restricting_account_format(username):
    config = source_config(
        base_url="https://team.atlassian.net/wiki", username=f"  {username}  "
    )
    assert config.effective_cloud is True
    assert config.username == username
    assert "effective_cloud" not in config.model_dump()


@pytest.mark.parametrize(
    "deployment,base_url",
    [
        ("data_center", "https://confluence.example.com"),
        ("data_center", "https://team.atlassian.net/wiki"),
        ("auto", "https://confluence.example.com"),
        ("auto", "https://team.atlassian.net.example.com"),
        ("auto", "https://notatlassian.net"),
        ("auto", "https://example.com/team.atlassian.net"),
    ],
)
def test_data_center_and_other_hosts_allow_token_only(deployment, base_url):
    config = source_config(deployment=deployment, base_url=base_url)
    assert config.effective_cloud is False
    assert config.username is None


def test_changing_existing_source_to_cloud_requires_username():
    original = source_config()
    with pytest.raises(ValidationError, match="Cloud requires an Atlassian account email"):
        SourceConfig.model_validate({**original.model_dump(), "deployment": "cloud"})
