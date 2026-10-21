from pathlib import Path


def test_ghcr_publication_workflow_contract():
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "docker-publish.yml"
    ).read_text()

    assert "branches: [main]" in workflow
    assert "types: [published]" in workflow
    assert "!github.event.release.prerelease" in workflow
    assert "contents: read" in workflow
    assert "packages: write" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "timeout-minutes: 60" in workflow
    assert 'echo "tag=latest"' in workflow
    assert r"^v[0-9]+\.[0-9]+\.[0-9]+$" in workflow
    assert "git merge-base --is-ancestor" in workflow
    assert "linux/amd64,linux/arm64" in workflow
    assert "ghcr.io/fpytloun/intaris:${{ steps.image.outputs.tag }}" in workflow
    assert "docker/login-action@v3" in workflow
    assert "docker/build-push-action@v6" in workflow
    assert "github.event.release.tag_name" in workflow.split("run: |", 1)[0]
