// Generates src/contracts/{material,blueprint,audit}.ts from the frozen JSON Schemas.
//
// Each target names its own pool. The schemas are deliberately not in one shared directory: the
// audit pool contains no blueprint schema, and that absence is the blindness boundary.
//
// Single source of truth (design.md §10): the frontend never hand-writes the
// three contract types, so "the blueprint the frontend believes in" cannot
// diverge from "the blueprint the backend emits".
//
//   node scripts/gen-contracts.mjs            # write
//   node scripts/gen-contracts.mjs --check    # fail if output would change

import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { compile } from 'json-schema-to-typescript'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '..', '..')
const outDir = resolve(here, '..', 'src/contracts')

// Spelled out rather than globbed: a glob matching two subjects would pick one by directory order
// and generate types for the wrong one.
const GENERATE_SCHEMAS = 'skills/generate/generate-listening-part1/schemas'
const AUDIT_SCHEMAS = 'skills/audit/audit-listening-part1/schemas'

const TARGETS = [
  { dir: GENERATE_SCHEMAS, schema: 'material.schema.json', out: 'material.ts', root: 'Material' },
  { dir: GENERATE_SCHEMAS, schema: 'blueprint.schema.json', out: 'blueprint.ts', root: 'Blueprint' },
  { dir: AUDIT_SCHEMAS, schema: 'audit.schema.json', out: 'audit.ts', root: 'Audit' },
]

const BANNER = [
  '/* eslint-disable */',
  '/**',
  ' * AUTO-GENERATED, DO NOT EDIT.',
  ' *',
  ' * Source: %SOURCE%',
  ' * Regenerate: npm run contracts:gen',
  ' */',
  '',
].join('\n')

async function render(target) {
  const schemaPath = resolve(repoRoot, target.dir, target.schema)
  const schema = JSON.parse(await readFile(schemaPath, 'utf8'))
  const body = await compile(schema, target.root, {
    bannerComment: '',
    additionalProperties: false,
    style: { semi: false, singleQuote: true },
  })
  return BANNER.replace('%SOURCE%', `${target.dir}/${target.schema}`) + body
}

const check = process.argv.includes('--check')
let drift = false

await mkdir(outDir, { recursive: true })
for (const target of TARGETS) {
  const next = await render(target)
  const outPath = resolve(outDir, target.out)
  if (check) {
    let current = ''
    try {
      current = await readFile(outPath, 'utf8')
    } catch {
      /* missing counts as drift */
    }
    if (current !== next) {
      drift = true
      console.error(`contracts drift: src/contracts/${target.out}`)
    }
  } else {
    await writeFile(outPath, next, 'utf8')
    console.log(`wrote src/contracts/${target.out}`)
  }
}

if (check && drift) {
  console.error('Run `npm run contracts:gen` and commit the result.')
  process.exit(1)
}
if (check) console.log('contracts up to date')
