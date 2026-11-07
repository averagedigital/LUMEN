# Public source capture

## Context
Public demo captures must replay without network access.

## Symptoms
Redirected resources escaped routing. Relative assets changed after serialization.

## What did not work
`route.continue()` and fulfilled redirects bypassed later route handlers. A zero server request count did not prove that Chromium attempted no request.

## Root cause
Redirect chains use browser networking. Replacing an existing base URL changes relative paths.

## Solution
Fetch bounded GET responses through curl and fulfill them locally. Reject HTTP redirects and require canonical URLs. Preserve `document.baseURI`. Validate raster headers before Chromium receives images.

## Why it works
Accepted responses never delegate network access to Chromium. Offline replay uses recorded resources and the original base URL.

## Verification
HTTPS fixtures cover redirect request events, chunked oversized scripts, raster dimensions, concurrent assets, missing images and exact replay.

## Prevention
Test browser request events as well as server requests. Require two stable source screenshots and exact offline PNG equality.

## Applies when
Curated anonymous public demos. Canvas-dependent or unstable captures are rejected. Captures remain unverified until dataset validation.
