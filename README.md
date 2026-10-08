# ethanroscoe.com

My portfolio site, its chat assistant, and the weekly skills radar. Live at [ethanroscoe.com](https://ethanroscoe.com).

- **Site:** one static page (`index.html`) on GitHub Pages, with light and dark mode.
- **Ask tab:** an assistant that answers questions about my experience. It runs on the Claude API behind a Cloudflare Worker that keeps the API key server-side, allows only this site's origin, and rate-limits requests.
- **Skills radar:** every Monday a GitHub Action pulls entry-level IT postings around Dallas–Fort Worth from the Adzuna API, Claude Haiku labels the skills in each one, and Python counts them into `data/radar.json`.

## Credits

Made with the help of [Claude Code](https://claude.com/claude-code), Anthropic's AI coding agent.
