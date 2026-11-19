import { expect, test } from 'vitest';
import caseSchema from '../json/case.schema.json';
import dataSchema from '../json/data.schema.json';
import documentSchema from '../json/document.schema.json';
import flowSchema from '../json/flow.schema.json';
import manifestSchema from '../json/manifest.schema.json';
import profileSchema from '../json/profile.schema.json';
import schemaPackage from '../package.json';
import { CONTRACT_STATUS, CONTRACT_VERSION, SCHEMA_IDS, SCHEMA_PACKAGE_VERSION } from './index';

const jsonSchemas = {
  case: caseSchema,
  data: dataSchema,
  document: documentSchema,
  flow: flowSchema,
  manifest: manifestSchema,
  profile: profileSchema,
};

test('exports version', () => {
  expect(SCHEMA_PACKAGE_VERSION).toBe('0.0.0');
});

test('package metadata uses the package version, not the draft contract version', () => {
  expect(schemaPackage).toMatchObject({ name: '@pulse/schema', version: SCHEMA_PACKAGE_VERSION });
  expect(SCHEMA_PACKAGE_VERSION).not.toBe(CONTRACT_VERSION);
});

test('exports the explicitly draft contract and its complete schema IDs', () => {
  expect(CONTRACT_VERSION).toBe('1-draft');
  expect(CONTRACT_STATUS).toBe('draft');
  expect(SCHEMA_IDS).toEqual({
    case: 'urn:pulse:schema:1-draft:case',
    data: 'urn:pulse:schema:1-draft:data',
    document: 'urn:pulse:schema:1-draft:document',
    flow: 'urn:pulse:schema:1-draft:flow',
    manifest: 'urn:pulse:schema:1-draft:manifest',
    profile: 'urn:pulse:schema:1-draft:profile',
  });
});

test('the generated document union uses the draft schema IDs as discriminators', () => {
  expect(documentSchema.discriminator).toEqual({
    propertyName: '$schema',
    mapping: {
      [SCHEMA_IDS.case]: '#/$defs/Case',
      [SCHEMA_IDS.data]: '#/$defs/DataSet',
      [SCHEMA_IDS.flow]: '#/$defs/Flow',
      [SCHEMA_IDS.manifest]: '#/$defs/Manifest',
      [SCHEMA_IDS.profile]: '#/$defs/Profile',
    },
  });
});

test.each(Object.entries(jsonSchemas))(
  'matches the %s JSON Schema file identity',
  (kind, schema) => {
    expect(SCHEMA_IDS).toHaveProperty(kind, schema.$id);
    expect(schema).toMatchObject({
      $id: `urn:pulse:schema:${CONTRACT_VERSION}:${kind}`,
      $schema: 'https://json-schema.org/draft/2020-12/schema',
      $comment: expect.stringContaining('Draft, not a frozen execution DSL'),
    });
  },
);
