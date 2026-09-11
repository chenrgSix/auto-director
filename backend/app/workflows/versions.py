"""Validation metadata must not invalidate a prepared story's workflow selection."""


def version_matches(profile, saved_version):
    return (
        type(saved_version) is int
        and profile.get("configuration_version", profile["version"])
        <= saved_version
        <= profile["version"]
    )


def versions_match(snapshot, profiles):
    return set(snapshot) == {p["id"] for p in profiles} and all(
        version_matches(p, snapshot.get(p["id"])) for p in profiles
    )
