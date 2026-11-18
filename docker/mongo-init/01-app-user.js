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

// Same rights as the Atlas custom role mi_app_rw, granted PER NAMED COLLECTION. A
// database-wide "insert" would also let the app create any collection (MongoDB's insert
// right includes the create command), so no collection-level right is database-wide.
const RW = ["find", "insert", "update", "remove", "listIndexes", "collStats"];
const on = (collection, actions) => ({ resource: { db: DB, collection }, actions });

admin.createRole({
  role: "mi_app_rw",
  privileges: [
    on("news_items", [...RW, "killAnyCursor"]),
    on("llm_extractions", RW),
    on("_healthcheck", ["find", "insert", "remove", "listIndexes", "collStats"]),
    // Append-only audits: no remove (updates are needed to add the outcome once).
    on("prediction_audit", ["find", "insert", "update", "listIndexes", "collStats"]),
    on("", ["listCollections", "dbStats", "killAnyCursor"]), // database-wide, no data rights
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
