---
layout: default
---

## Why these repositories exist

Maintain copies of every benchmark design repository in the GitHub org
`wafer-space-bench`, with provenance, tapeout tags and a consistent branch
layout, and publish an index of them. The tooling lives in the public repo
[wafer-space/wafer.space-bench](https://github.com/wafer-space/wafer.space-bench).

Every bench repo has the same branch namespaces:

| Ref | Meaning |
|---|---|
| `about` | Default branch. Orphan history. Holds `bench.yaml` (source, kind, why, licence, tapeouts, branches, generated time) and `README.md` generated from it. |
| `upstream/<branch>` | Exact mirror of the upstream branch. Only the sync job writes it. |
| `tapeout/<org>/<run>/<slot>` | Frozen. Same commit as the tag of the same name. |
| `dev/repro/<org>/<run>/<slot>` | Reproduce the original result with the original pinned tools. |
| `dev/upgrade/<org>/<run>/<slot>` | Latest development LibreLane + gf180mcu PDK. |
| `dev/process/<config>/<org>/<run>/<slot>` | Alternative process config (`m035n3`, `i135x5`, ...). |
| `dev/<topic>` | Other development. |

## Repositories

{% assign repos = site.data.repos %}
{% assign n_laid = repos | where: "state", "laid-out" | size %}
{% assign n_created = repos | where: "state", "created" | size %}
{% assign n_pending = repos | where: "state", "pending" | size %}
{% assign n_nosrc = repos | where: "state", "no-source" | size %}

{{ repos | size }} repositories: {{ n_laid }} laid out, {{ n_created }} created, {{ n_pending }} pending, {{ n_nosrc }} with no known source.

<p>
  <label for="repo-filter">Filter:</label>
  <input id="repo-filter" type="search" placeholder="name, upstream, run, licence...">
  <span id="repo-count"></span>
</p>

<table id="repo-table">
  <thead>
    <tr><th>Repo</th><th>Upstream</th><th>Strategy</th><th>State</th><th>Runs</th><th>Tapeouts</th><th>Licence</th></tr>
  </thead>
  <tbody>
{% for repo in repos %}
    <tr>
      <td>{% if repo.url != "" %}<a href="{{ repo.url | escape }}">{{ repo.name | escape }}</a>{% else %}{{ repo.name | escape }}{% endif %}</td>
      <td>{% if repo.upstream != "" %}<a href="{{ repo.upstream | escape }}">{{ repo.upstream | escape }}</a>{% if repo.original_upstream != repo.upstream %} (was {{ repo.original_upstream | escape }}){% endif %}{% else %}no known repository{% endif %}</td>
      <td>{{ repo.strategy | escape }}</td>
      <td>{{ repo.state | escape }}</td>
      <td>{{ repo.runs | join: ", " | escape }}</td>
      <td title="{{ repo.tapeouts | join: ", " | escape }}">{{ repo.tapeouts | size }}</td>
      <td>{{ repo.licence | escape }}</td>
    </tr>
{% endfor %}
  </tbody>
</table>

<script src="{{ '/assets/table.js' | relative_url }}"></script>
