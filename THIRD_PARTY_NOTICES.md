# Third-party notices

The Apache 2.0 license in this repository applies to Cast2Sendspin's original code and
icon. Dependencies retain their own licenses and copyright notices.

## DIAL / SSDP

This application uses Patrick Kan's
[yt-cast-receiver](https://github.com/patrickkfkan/yt-cast-receiver) (MIT), including
the forks [peer-dial](https://github.com/patrickkfkan/peer-dial) and
[peer-ssdp](https://github.com/patrickkfkan/peer-ssdp).

The DIAL/SSDP packages contain LGPL-3.0 license files. The `peer-dial` README
also states **“Free for non commercial use”**. That wording is not an ordinary
LGPL restriction and requires clarification from upstream before relying on
this dependency for commercial distribution or use. This project's Apache 2.0 license
does not remove or resolve that upstream ambiguity.

Their JavaScript source and upstream notices are included under `node_modules/`
in the container. The container is rebuildable with replacement versions using
the supplied Dockerfile and npm lockfile.

## Audio and runtime

The image also includes YouTube.js, QuickJS/quickjs-emscripten, Sendspin,
Music Assistant's Python client, yt-dlp and its dependencies, FFmpeg, Node.js,
Python, and Debian runtime libraries. Their notices accompany the installed
packages; Debian copyright files are under `/usr/share/doc`.

YouTube and YouTube Music are Google trademarks. Music Assistant and Sendspin
are independent projects. No affiliation or endorsement is implied.
