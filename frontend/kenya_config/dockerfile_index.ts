// This file is COPIED into PRISM's src/config/kenya/index.ts during the
// frontend image build (Dockerfile.frontend). Mirrors the shape that
// infra/deploy-all.sh phase 4 uses when injecting kenya_config into a
// freshly cloned prism-app.
//
// It's separate from frontend/kenya_config/index.ts (which is the
// local-dev variant for running prism-app outside Docker) because the
// two consumers wrap appConfig/rawLayers differently.

import appConfig from './prism.json';
import rawLayers from './layers.json';

const translation = {
  en: { 'Admin 1': 'Province', 'Admin 2': 'District', 'Admin 3': 'Ward' },
};
const rawTables = {};
const rawReports = {};

export default {
  appConfig,
  rawLayers,
  rawReports,
  rawTables,
  translation,
  defaultBoundariesFile: 'ken_bnd_adm3_WFP.json',
};
