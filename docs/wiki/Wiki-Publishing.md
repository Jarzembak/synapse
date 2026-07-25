# Wiki publishing

The Markdown files in `docs/wiki/` are the version-controlled documentation
source. Keeping them in the main repository allows documentation changes to be
reviewed in the same pull request as the feature they describe.

GitHub wikis are separate Git repositories. Updating `docs/wiki/` does not
automatically publish those files to the Wiki tab.

## One-time initialization

The Synapse repository has the Wiki feature enabled, but GitHub does not create
`Jarzembak/synapse.wiki.git` until an initial page is saved.

1. Open `https://github.com/Jarzembak/synapse/wiki`.
2. Create the first page.
3. Name it `Home`.
4. Save it.

After that, this succeeds:

```bash
git clone https://github.com/Jarzembak/synapse.wiki.git
```

GitHub documents this requirement in
[Adding or editing wiki pages](https://docs.github.com/en/communities/documenting-your-project-with-wikis/adding-or-editing-wiki-pages).

## Publish the prepared pages

After the wiki repository exists:

1. Clone `https://github.com/Jarzembak/synapse.wiki.git`.
2. Copy the contents of the main repository's `docs/wiki/` directory into the
   root of the wiki clone.
3. Review internal links and the generated sidebar.
4. Commit in the wiki repository.
5. Push its default branch.

GitHub derives page titles from filenames. Files such as
`Getting-Started.md` become pages such as `Getting Started`; `_Sidebar.md`
provides navigation.

Only changes pushed to the wiki repository's default branch become visible to
readers.

## Ongoing maintenance

Treat `docs/wiki/` as canonical:

1. update it with the application change;
2. review and merge it through the normal pull-request workflow; and
3. copy the merged pages to the wiki repository.

Avoid editing the published Wiki and `docs/wiki/` independently, because
GitHub provides no built-in synchronization or conflict resolution between the
two repositories.

An automated publication workflow can be added later. It should copy only
`docs/wiki/*.md` from the main branch into the wiki repository using a
credential with narrowly scoped write access.
