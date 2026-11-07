# Security Policy

## Supported versions

Print2PACS ships as a self-updating Windows executable. Only the **latest
release** receives security fixes — older versions should update to the newest
build (use the in-app updater, or download it from the
[Releases page](https://github.com/towbinaj/Print2PACS/releases/latest)).

| Version        | Supported |
| -------------- | :-------: |
| Latest release |     ✅     |
| Older releases |     ❌     |

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Public issues are
visible to everyone, including before a fix exists.

Report it privately through GitHub instead:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Include the affected version, a description, and steps to reproduce.

This opens a private advisory that only the maintainer can see, where the issue
can be discussed and patched before public disclosure.

If you would rather use email, or GitHub private reporting is not available to
you, contact **DICOMTool@gmail.com**. Please do not include exploit details in a
public channel.

## Scope

Print2PACS converts files to DICOM and stores them to a PACS/VNA you configure.
Security-relevant areas include, for example:

- Parsing of untrusted input files (images, PDF, video, and dropped DICOM
  objects).
- Re-filing of external DICOM objects (the preamble is zeroed and the object is
  rebuilt to neutralize DICOM/PE polyglots; see the README).
- The self-update mechanism (release download and SHA-256 integrity check).

Configuration (AE titles, hosts) and any PHI live only in the local
`Print2PACS-data` folder and are never sent anywhere except the DICOM
destinations you configure.

## What to expect

- Acknowledgement of a valid report as soon as the maintainer is able.
- A fix in the next release once confirmed, with credit if you would like it.
- Please allow time for a fix to ship before any public disclosure.
