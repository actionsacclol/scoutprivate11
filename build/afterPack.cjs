// electron-builder afterPack hook — flip Electron Fuses on the packaged binary.
//
// Fuses are bits burned into the Electron executable itself (not config a
// repacked asar can change), so they hold even after an attacker unpacks and
// edits app.asar. The one that matters most:
//
//   RunAsNode = false
//     Without it, `set ELECTRON_RUN_AS_NODE=1 && "Krypt Scout.exe" -e "<js>"`
//     turns the shipped app into a Node REPL running INSIDE the app's own
//     context — full filesystem access, your IPC surface, and (because DPAPI
//     unwraps for the same Windows user) the power to decrypt the saved
//     Polymarket wallet key. Disabling it is the single highest-value harden
//     for this app, and this app never runs itself as Node (the backend is a
//     separate PyInstaller exe), so there is zero functionality cost.
//
// The rest close adjacent holes: env-var / CLI debugger injection, loading a
// swapped-in app directory, and unencrypted cookies. All safe here — verified
// against electron/system/python-backend.ts, which spawns the Python backend,
// never process.execPath.
//
// EmbeddedAsarIntegrityValidation (tamper-proof the asar) is gated behind
// SCOUT_ASAR_INTEGRITY=1: it requires electron-builder to inject the asar
// header hash into the binary, which is platform/version-sensitive, so it must
// be verified to still LAUNCH before it becomes the default. Turn it on once a
// packaged build boots clean with it set.
const path = require('node:path');

exports.default = async function afterPack(context) {
  const { flipFuses, FuseVersion, FuseV1Options } = require('@electron/fuses');
  const { appOutDir, packager, electronPlatformName } = context;
  const appName = packager.appInfo.productFilename;

  // The Electron binary whose fuses we flip — inside the .app bundle on macOS,
  // the top-level exe on Windows/Linux.
  const target =
    electronPlatformName === 'darwin'
      ? path.join(appOutDir, `${appName}.app`, 'Contents', 'MacOS', appName)
      : path.join(
          appOutDir,
          electronPlatformName === 'win32' ? `${appName}.exe` : appName,
        );

  const wantIntegrity = process.env.SCOUT_ASAR_INTEGRITY === '1';

  const fuses = {
    version: FuseVersion.V1,
    // Strip legitimate on the sacrificial altar of "no arbitrary code exec".
    [FuseV1Options.RunAsNode]: false,
    [FuseV1Options.EnableNodeOptionsEnvironmentVariable]: false,
    [FuseV1Options.EnableNodeCliInspectArguments]: false,
    // Load only the packed asar, never a swapped-in unpacked app/ directory.
    [FuseV1Options.OnlyLoadAppFromAsar]: true,
    // Encrypt the cookie store on disk.
    [FuseV1Options.EnableCookieEncryption]: true,
    // Opt-in (see header): validate the asar hash against the one burned into
    // the binary, so an edited app.asar refuses to load.
    [FuseV1Options.EnableEmbeddedAsarIntegrityValidation]: wantIntegrity,
    // Flipping fuses invalidates the macOS code signature; re-apply the ad-hoc
    // one so the app still launches (a real signing step, if configured, runs
    // afterSign and re-signs properly on top).
    resetAdHocDarwinSignature: electronPlatformName === 'darwin',
  };

  await flipFuses(target, fuses);
  // eslint-disable-next-line no-console
  console.log(
    `  • fuses flipped on ${path.basename(target)} ` +
      `(RunAsNode=off, asarIntegrity=${wantIntegrity ? 'on' : 'off'})`,
  );
};
