// Generates src/mocks/fixtures/generated.ts from the REAL fixtures produced by
// 07-28-skill-contract (shared/tests/fixtures/*.json).
//
// Why generate instead of `import x from '...json'`: a JSON import widens
// `"listening_material"` to string and `[]` to never[], so it can never be
// checked against the contract types. Emitting an annotated TS literal
// (`export const M: Material = {...}`) makes tsc verify the real fixture data
// against the generated schema types — which is the first line of defence
// implement.md phase 2 asks for.
//
//   node scripts/gen-fixtures.mjs           # write
//   node scripts/gen-fixtures.mjs --check   # fail if output would change

import { readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '..', '..')
const fxDir = resolve(repoRoot, 'skills/ielts-listening-skills/shared/tests/fixtures')
const outPath = resolve(here, '..', 'src/mocks/fixtures/generated.ts')

const FILES = [
  { file: 'material_valid.json', name: 'MATERIAL_VALID', type: 'Material' },
  { file: 'blueprint_valid.json', name: 'BLUEPRINT_VALID', type: 'Blueprint' },
  { file: 'blueprint_bad_anchor.json', name: 'BLUEPRINT_BAD_ANCHOR', type: 'Blueprint' },
  { file: 'audit_valid.json', name: 'AUDIT_VALID', type: 'Audit' },
  { file: 'audit_aligned.json', name: 'AUDIT_ALIGNED', type: 'Audit' },
]

const chunks = [
  `/* eslint-disable */
/**
 * AUTO-GENERATED, DO NOT EDIT.
 *
 * Source: skills/ielts-listening-skills/shared/tests/fixtures/
 * Regenerate: npm run fixtures:gen
 *
 * These are the real fixtures from 07-28-skill-contract, not invented data.
 * The explicit type annotations make tsc verify them against the frozen
 * schema-generated contract types.
 */
import type { Audit, Blueprint, Material } from '@/contracts'
`,
]

for (const t of FILES) {
  const data = JSON.parse(await readFile(resolve(fxDir, t.file), 'utf8'))
  chunks.push(`\nexport const ${t.name}: ${t.type} = ${JSON.stringify(data, null, 2)}\n`)
}

const next = chunks.join('')

if (process.argv.includes('--check')) {
  let current = ''
  try {
    current = await readFile(outPath, 'utf8')
  } catch {
    /* missing counts as drift */
  }
  if (current !== next) {
    console.error('fixtures drift: src/mocks/fixtures/generated.ts')
    console.error('Run `npm run fixtures:gen` and commit the result.')
    process.exit(1)
  }
  console.log('fixtures up to date')
} else {
  await writeFile(outPath, next, 'utf8')
  console.log(`wrote src/mocks/fixtures/generated.ts (${FILES.length} fixtures)`)
}
