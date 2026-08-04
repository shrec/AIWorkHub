# Search verification setup

The deployed canonical site is `https://shrec.github.io/AIWorkHub/`.

After GitHub Pages is live:

1. Add the property to Google Search Console and Bing Webmaster Tools.
2. Put the verification `<meta>` values in `site/index.html`; do not commit a
   fabricated value.
3. Submit `https://shrec.github.io/AIWorkHub/sitemap.xml` to both services.
4. Request indexing for the home page and the product pages listed in the
   sitemap.
5. Use the landing page as the canonical product link in Marketplace, GitHub,
   articles and directory submissions.

The repository workflow deploys the site, but search-engine verification and
indexing requests require the repository owner's Search Console accounts.
