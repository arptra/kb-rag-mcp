# Vendored browser dependency

`markdown-it.min.js` is the unmodified UMD browser bundle from **markdown-it 15.0.3**,
`package/dist/browser/markdown-it.umd.min.js` in the official npm package.

- Package: https://registry.npmjs.org/markdown-it/-/markdown-it-15.0.3.tgz
- Verified package integrity: `sha512-+Az7Ojz3iRGu8KYMvJ6xVhvrxP3eE+vEtJX75Ow88XBpcQNV/0lGX5ZynIIBftREo4DC2jsveAMDaeltmm8QvQ==`
- License: `LICENSE.markdown-it.txt` (MIT).

Harness uses parsing tokens with HTML disabled and renders them as DOM text/elements.
The browser does not fetch this dependency from an external CDN at runtime.
