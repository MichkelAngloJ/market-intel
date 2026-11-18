// Runs once (mongo image entrypoint, as root) on first start with an empty data volume.
// Creates the same two logins as on Atlas, both stored in the admin database
// (connect with ?authSource=admin):
//   - the app user (MONGO_APP_USER) with the custom role mi_app_rw: read and write documents,
//     but no creating or dropping collections and no index or validator changes;
//   - the setup user (MONGO_SETUP_USER) with readWrite + dbAdmin on market_intel, used only by
//     "uv run python -m market_intel.mongo_setup".
const DB = "market_intel";

function required(name) {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} must be set in docker/compose.env`);
  }
  return value;
}

const admin = db.getSiblingDB("admin");

// Same actions as the Atlas custom role mi_app_rw.
admin.createRole({
  role: "mi_app_rw",
  privileges: [
    {
      resource: { db: DB, collection: "" },
      actions: [
        "find",
        "insert",
        "update",
        "remove",
        "listIndexes",
        "collStats",
        "killAnyCursor",
        "listCollections",
        "dbStats",
      ],
    },
  ],
  roles: [],
});

admin.createUser({
  user: required("MONGO_APP_USER"),
  pwd: required("MONGO_APP_PASSWORD"),
  roles: [{ role: "mi_app_rw", db: "admin" }],
});

admin.createUser({
  user: required("MONGO_SETUP_USER"),
  pwd: required("MONGO_SETUP_PASSWORD"),
  roles: [
    { role: "readWrite", db: DB },
    { role: "dbAdmin", db: DB },
  ],
});

print("created role mi_app_rw, the app user and the setup user");
