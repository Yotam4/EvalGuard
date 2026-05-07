/// <reference types="@testing-library/jest-dom" />

// This file is purely for TypeScript: it pulls the
// ``@testing-library/jest-dom`` matcher type augmentations into the
// test-file type-check pass. Without it, ``tsc --noEmit`` flags
// every ``expect(el).toBeInTheDocument()`` as "Property does not
// exist on type 'Assertion<HTMLElement>'" — even though the
// matchers ARE registered at runtime via ``vitest.setup.ts``.
//
// The runtime registration happens in vitest.setup.ts; this is the
// matching compile-time hook.
