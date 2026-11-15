# Actionlint executable dependency assessment

Reviewed 2026-11-15. Reassess by 2027-02-15, or earlier when the actionlint source revision or assessed dependency changes.

## Scope and result

The official pre-commit hook is pinned to actionlint v1.7.12, commit `914e7df21a07ef503a81201c76d2b11c789d3fca`. Its entrypoint executes `cmd/actionlint`. The upstream Go module also contains maintenance generators and tests; scanning every module in that graph reported dependencies which this executable does not import.

The inventory now takes the union of modules imported by `./cmd/actionlint` on Linux, macOS and Windows, each on amd64 and arm64, with cgo both disabled and enabled. The current union contains 12 modules; the complete 18-module graph is retained separately for inspection. The hook installer may build other upstream commands through `go install ./...`, but those maintenance commands are not executed by the hook. This assessment and the blocking inventory cover the actionlint executable, not arbitrary use of those other commands.

`golang.org/x/crypto`, `golang.org/x/net` and `golang.org/x/text` are absent from the executable's dependency graph on every target. In particular, upstream's `x/net/html` import belongs to the maintenance scraper `scripts/generate-webhook-events/main.go`. Removing these from the executable inventory is a scope correction; their versions are not declared fixed or globally exempted.

## One assessed advisory

[GO-2026-5024 / CVE-2026-39824](https://pkg.go.dev/vuln/GO-2026-5024) concerns integer overflow in `golang.org/x/sys/windows.NewNTUnicodeString`, fixed upstream in x/sys v0.44.0. Actionlint v1.7.12 still uses x/sys v0.42.0; the inventory records that actual version.

The Windows package is imported for terminal/color support, but the affected function is not called by this actionlint command. Govulncheck v1.9.0 with Go 1.27.4 reported zero reachable vulnerable symbols for all six OS/architecture combinations. On Windows it identified the affected package as imported but found no call to the vulnerable symbol. On Linux and macOS the Windows package was not imported. Comparing `go list` metadata with cgo enabled and disabled selected identical non-standard-library import paths, module versions, Go files and Cgo files for this exact revision.

The approved exception is recorded in [`actionlint-assessment.toml`](../../actionlint-assessment.toml). Inventory generation checks the exact actionlint SHA, x/sys version and expiry before deriving an effective OSV policy that ignores only GO-2026-5024 and its aliases. It does not ignore x/sys as a package or any other advisory, and license checks remain active. The root OSV policy has no vulnerability exclusion for this advisory. A changed pin, changed or absent assessed dependency, expired assessment, failed dependency query, or inconsistent module inputs fails generation and removes stale generated policy/inventory files.

## Reproducing and renewing the assessment

Use a disposable checkout of the exact actionlint SHA and the Go version recorded above. Install the assessment tool into a temporary `GOBIN` with `go install golang.org/x/vuln/cmd/govulncheck@v1.9.0`; it is maintainer tooling, not a contributor prerequisite. The tool module checksum used for this assessment was `h1:12VaqnI9kJfqwRoPptxoqsUhQtdfrSbdksT7X/KY/sc=`.

Run the following for each `GOOS` value (`linux`, `darwin`, `windows`) and `GOARCH` value (`amd64`, `arm64`), using the temporary govulncheck executable on `PATH`:

```sh
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 GOWORK=off GOFLAGS=-mod=readonly GOTOOLCHAIN=local govulncheck -show=verbose ./cmd/actionlint
```

Compare the non-standard-library dependency metadata with both cgo settings:

```sh
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 GOWORK=off GOTOOLCHAIN=local go list -mod=readonly -deps -json ./cmd/actionlint
GOOS=windows GOARCH=amd64 CGO_ENABLED=1 GOWORK=off GOTOOLCHAIN=local go list -mod=readonly -deps -json ./cmd/actionlint
```

Record the commands, tool versions, selected source revision and findings when reassessing. If x/sys is fixed, remove the exception and adapt the generator to produce the base policy without it; do not merely copy the old exception to a new revision. If the vulnerable symbol becomes reachable, upgrade or patch the actual tool before accepting the change. This is a dated source-level assessment, not proof of safety for every locally installed Go toolchain or custom build configuration.
