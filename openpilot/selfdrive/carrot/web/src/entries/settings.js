import { installSettingsRuntimeFacade } from "../features/settings/index.js";

installSettingsRuntimeFacade(globalThis);

import { installObdSettings } from "../features/settings/obd.js";
installObdSettings();
