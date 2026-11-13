// Runs once (mongo image entrypoint, as root) on first start with an empty data volume.
// Creates the app user with readWrite on market_intel only - the same least-privilege role
// as the Atlas user. The app connects with ?authSource=market_intel.
const user = process.env.MONGO_APP_USER;
const pwd = process.env.MONGO_APP_PASSWORD;
if (!user || !pwd) {
  throw new Error("MONGO_APP_USER and MONGO_APP_PASSWORD must be set in docker/compose.env");
}
db.getSiblingDB("market_intel").createUser({
  user: user,
  pwd: pwd,
  roles: [{ role: "readWrite", db: "market_intel" }],
});
print("created app user with readWrite@market_intel");
