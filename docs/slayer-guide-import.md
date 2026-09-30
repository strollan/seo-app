# Local Slayer guide import

This command creates only local, source-controlled LeadMeLeads artifacts. It
does not call a network service, deploy, restart a server, commit, push Git,
or record an article as publicly published.

Run it from the LeadMeLeads checkout:

```bash
python -m app.imported_guides --package-dir /absolute/path/to/slayer-package
```

The package must contain `slayer-guide.json` and images below that same package
directory. Paths are relative, may not escape the package directory, and must
be PNG, JPEG, GIF, or WebP files whose bytes match the declared image type.

```json
{
  "slug": "spot-inconsistent-business-details-lead-research",
  "title": "How to Spot Inconsistent Business Details During Lead Research",
  "dek": "A practical way to compare the public details a prospect presents.",
  "body_sections": [
    {
      "heading": "Start with the business's own site",
      "paragraphs": ["Compare the contact and service details you can observe."],
      "bullets": ["Record facts before making an outreach decision."]
    }
  ],
  "hero_image": {
    "path": "assets/hero.png",
    "alt": "A business profile checked against contact and location details."
  },
  "inline_images": [
    {
      "path": "assets/checklist.png",
      "alt": "A checklist for comparing public business details.",
      "placement_after_section": 0
    }
  ],
  "seo_title": "Spot Inconsistent Business Details During Lead Research | LeadMeLeads",
  "meta_description": "A practical guide to comparing public business details before outreach.",
  "resource_card": {
    "topic": "verification",
    "summary": "Compare public business details before treating a prospect as verified."
  }
}
```

On success, the importer writes:

- `app/content/imported_guides/<slug>.json`
- `app/static/images/resources/imported/<slug>/hero.<extension>`
- `app/static/images/resources/imported/<slug>/inline-N.<extension>`

It refuses a slug owned by an existing hand-built public guide, a reserved app
route, or another imported record before it writes anything. The resulting
local route is `http://127.0.0.1:8800/<slug>`.
