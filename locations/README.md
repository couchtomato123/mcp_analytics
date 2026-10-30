# Locations

One folder per location. The weekly drafting job (`python -m social_content generate`)
writes posts for every folder whose `profile.yaml` says `active: true`.

```
locations/
  _brand.yaml                       company-wide voice and rules
  p44-comerica-bank-building/
    profile.yaml                    edit this: reviewers, platforms, voice, extra facts
    salesforce.yaml                 address + amenities from Salesforce (regenerated; don't edit)
    photos/                         optional photos committed to git
    photos.yaml                     optional captions: {"lobby.jpg": "Lobby with reception desk"}
```

## Turning a location on

1. Open its `profile.yaml` on GitHub and click the pencil icon.
2. Add the reviewer emails under `reviewers:` (one per line, e.g. `  - jane@barrister-suites.com`).
3. Write a sentence or two of `voice` and any `extra_facts` (current offers, events, what's special).
4. Set `active: true` and commit. Railway redeploys automatically.
5. Photos: reviewers can upload them in the app (**Photo library**). Or drag them into the
   `photos/` folder on GitHub (**Add file → Upload files**).

## Adding a new location

Create it as a Property in Salesforce and run `python -m social_content seed-from-salesforce`,
or copy an existing folder and edit it.
