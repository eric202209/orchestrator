# Frontend Completion Repair Artifacts

Use this guidance when a completion repair is specifically for a JavaScript or TypeScript frontend project.

Common repair artifacts include:

- source files under `src/`
- test files under `tests/`
- `vitest.config.ts`
- `jest.config.js`
- `package.json`
- `tsconfig.json`
- `.env.example`

Completion repair output should report the files it changed. Runtime completion code should use reported changed files directly instead of hardcoding this frontend-specific artifact list.
