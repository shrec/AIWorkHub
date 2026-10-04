# Vendored dependencies

The dependencies are vendored so the build is hermetic. Validation runs in the
sandboxed lane with no network and no absolute host paths. Every source was
copied unmodified from the vcpkg-verified download archive listed below. To
update one, replace the directory from the new upstream archive and update its
row here.

| Dir | Upstream | Version | Archive sha256 | Files kept |
|---|---|---|---|---|
| sqlite3 | sqlite.org autoconf amalgamation | 3.50.4 (3500400) | a3db587a1b92ee5ddac2f66b3edb41b26f9c867275782d46c3a088977d6a5b18 | sqlite3.c, sqlite3.h, sqlite3ext.h (public domain) |
| nlohmann_json | github.com/nlohmann/json | v3.12.0 | 4b92eb0c06d10683f7447ce9406cb97cd4b453be18d7279320f7b2f025c10187 | single_include/nlohmann/json.hpp, LICENSE.MIT |
| cli11 | github.com/CLIUtils/CLI11 | v2.6.1 | 377691f3fac2b340f12a2f79f523c780564578ba3d6eaf5238e9f35895d5ba95 | include/CLI/**, LICENSE |
| catch2 | github.com/catchorg/Catch2 | v3.11.0 | 82fa1cb59dc28bab220935923f7469b997b259eb192fb9355db62da03c2a3137 | extras/catch_amalgamated.{hpp,cpp}, LICENSE.txt |
| spdlog | github.com/gabime/spdlog | v1.16.0 | 8741753e488a78dd0d0024c980e1fb5b5c85888447e309d9cb9d949bdb52aa3e | include/spdlog/** (use with SPDLOG_USE_STD_FORMAT), LICENSE |
| tree_sitter | github.com/tree-sitter/tree-sitter | v0.25.10 | ad5040537537012b16ef6e1210a572b927c7cdc2b99d1ee88d44a7dcdc3ff44c | lib/src/**, lib/include/** (build via lib/src/lib.c), LICENSE |

The tree-sitter language grammars are added under `grammars/<lang>/` in phase
P2, pinned the same way.
