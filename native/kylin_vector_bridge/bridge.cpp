// Thin ABI adapter. All database operations go through the system SDK.
#include "bridge.h"
#include <Database.h>
#include <nlohmann/json.hpp>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <stdexcept>

using Json = nlohmann::json;
using namespace VectorDB;
namespace {
struct Context {
    std::shared_ptr<Database> db;
    std::mutex mutex;
    bool connected = false;
};
struct SdkError : std::runtime_error {
    int code;
    explicit SdkError(const Status& status)
        : std::runtime_error(status.Message()), code(static_cast<int>(status.Code())) {}
};
void checked(const Status& status) {
    if (!status.IsOk()) throw SdkError(status);
}
template <typename FieldT>
FieldDataPtr column(const std::string& name, const Json& rows) {
    auto field = std::make_shared<FieldT>(name);
    for (const auto& row : rows) {
        auto value = row.at(name).get<typename FieldT::ElementT>();
        if (field->Add(value) != StatusCode::OK)
            throw std::invalid_argument("Invalid field: " + name);
    }
    return field;
}
std::vector<FieldDataPtr> columns(const Json& request) {
    const auto& rows = request.at("rows");
    if (!rows.is_array() || rows.empty()) throw std::invalid_argument("rows must be nonempty");
    std::vector<FieldDataPtr> result;
    for (const auto& spec : request.at("fields")) {
        auto name = spec.at("name").get<std::string>();
        switch (static_cast<DataType>(spec.at("type").get<int>())) {
        case DataType::VARCHAR: result.push_back(column<VarCharFieldData>(name, rows)); break;
        case DataType::BOOL: result.push_back(column<BoolFieldData>(name, rows)); break;
        case DataType::INT64: result.push_back(column<Int64FieldData>(name, rows)); break;
        case DataType::FLOAT: result.push_back(column<FloatFieldData>(name, rows)); break;
        case DataType::JSON: result.push_back(column<JsonFieldData>(name, rows)); break;
        case DataType::FLOAT_VECTOR: result.push_back(column<FloatVecFieldData>(name, rows)); break;
        default: throw std::invalid_argument("Unsupported memory field type");
        }
    }
    if (result.empty()) throw std::invalid_argument("fields must be nonempty");
    return result;
}
template <typename FieldT>
Json value_at(const FieldDataPtr& field, size_t index) {
    return std::static_pointer_cast<FieldT>(field)->Data().at(index);
}
Json rows_from(const std::vector<FieldDataPtr>& fields) {
    Json rows = Json::array();
    if (fields.empty()) return rows;
    for (size_t i = 0; i < fields.front()->Count(); ++i) {
        Json row = Json::object();
        for (const auto& field : fields) {
            if (field->Count() != fields.front()->Count())
                throw std::runtime_error("SDK returned inconsistent column lengths");
            Json value;
            switch (field->Type()) {
            case DataType::VARCHAR: value = value_at<VarCharFieldData>(field, i); break;
            case DataType::BOOL: value = value_at<BoolFieldData>(field, i); break;
            case DataType::INT64: value = value_at<Int64FieldData>(field, i); break;
            case DataType::FLOAT: value = value_at<FloatFieldData>(field, i); break;
            case DataType::JSON: value = value_at<JsonFieldData>(field, i); break;
            case DataType::FLOAT_VECTOR: value = value_at<FloatVecFieldData>(field, i); break;
            default: throw std::runtime_error("Unsupported SDK output type");
            }
            row[field->Name()] = std::move(value);
        }
        rows.push_back(std::move(row));
    }
    return rows;
}
ConnectParam make_connect_param(const std::string& endpoint) {
    // Installed client 1.2.0.0-0k0.7: ConnectParam(appId) builds
    // unix:/tmp/kylin-ai-vector-engine-<geteuid>.sock. There is no default ctor,
    // Host(), or SetUri(). The three-argument ctor is TCP host+port (marked for test).
    ConnectParam defaults(std::string("mokoa"));
    if (endpoint.empty()) return defaults;
    if (endpoint.rfind("unix:", 0) == 0) {
        if (endpoint != defaults.Uri())
            throw std::invalid_argument("SDK has no URI setter; unset endpoint to use " + defaults.Uri());
        return defaults;
    }
    const auto colon = endpoint.rfind(':');
    if (colon == std::string::npos || colon == 0 || colon + 1 == endpoint.size())
        throw std::invalid_argument("endpoint must be empty, the SDK unix socket, or host:port");
    const auto host = endpoint.substr(0, colon);
    const auto port = std::stoi(endpoint.substr(colon + 1));
    if (port < 0 || port > 65535) throw std::invalid_argument("invalid TCP port");
    return ConnectParam(std::string("mokoa"), host, static_cast<uint16_t>(port));
}
Json dispatch(Context& ctx, const Json& req) {
    const auto op = req.at("op").get<std::string>();
    if (op == "connect") {
        if (ctx.connected) throw std::invalid_argument("Already connected");
        auto db = Database::Create();
        if (!db) throw std::runtime_error("SDK returned null database");
        auto param = make_connect_param(req.value("endpoint", std::string{}));
        param.SetConnectTimeout(req.value("timeout_ms", 5000u));
        checked(db->Connect(param));
        ctx.db = std::move(db);
        ctx.connected = true;
        return {{"backend", "kylin-vector-sdk"}, {"endpoint", param.Uri()}, {"app_id", param.AppId()}};
    }
    if (!ctx.connected) throw std::runtime_error("Not connected");
    auto collection = req.at("collection").get<std::string>();
    if (collection.empty()) throw std::invalid_argument("Empty collection name");
    if (op == "has_collection") {
        bool exists = false;
        // Never treat every SERVER_FAILED as collection-not-found (demo does).
        checked(ctx.db->HasCollection(collection, exists));
        return exists;
    }
    if (op == "create_collection") {
        CollectionSchema schema(collection, "MOKOA memory", false);
        for (const auto& spec : req.at("fields")) {
            FieldSchema field(spec.at("name").get<std::string>(),
                              static_cast<DataType>(spec.at("type").get<int>()), "",
                              spec.value("is_primary", false), false);
            if (spec.contains("max_length")) field.SetMaxLength(spec.at("max_length").get<uint32_t>());
            if (spec.contains("dim")) field.SetDimension(spec.at("dim").get<uint32_t>());
            if (!schema.AddField(field)) throw std::invalid_argument("Duplicate schema field");
        }
        IndexDesc index("embedding", "embedding_index", IndexType::FLAT, MetricType::COSINE);
        checked(ctx.db->CreateCollection(schema, index));
        return true;
    }
    if (op == "load_collection") {
        checked(ctx.db->LoadCollection(collection));
        return true;
    }
    if (op == "insert" || op == "upsert") {
        auto fields = columns(req);
        DmlResults result;
        checked(op == "insert" ? ctx.db->Insert(collection, fields, result)
                               : ctx.db->Upsert(collection, fields, result));
        return true;
    }
    if (op == "delete") {
        const auto expression = req.at("filter").get<std::string>();
        if (expression.empty()) throw std::invalid_argument("Refusing empty delete filter");
        DmlResults result;
        checked(ctx.db->Delete(collection, expression, result));
        return true;
    }
    if (op == "query") {
        QueryArguments args;
        checked(args.SetCollectionName(collection));
        checked(args.SetExpression(req.at("filter").get<std::string>()));
        for (const auto& field : req.at("output_fields")) checked(args.AddOutputField(field.get<std::string>()));
        checked(args.SetGuaranteeTimestamp(GuaranteeStrongTs()));
        QueryResults result;
        checked(ctx.db->Query(args, result, req.value("timeout_ms", 5000)));
        // This SDK has no query limit/offset API. Caller may trim, but must not
        // interpret trimming as server-side pagination or proof of completeness.
        return rows_from(result.OutputFields());
    }
    if (op == "search") {
        SearchArguments args;
        checked(args.SetCollectionName(collection));
        checked(args.SetTopK(req.at("top_k").get<int64_t>()));
        checked(args.SetMetricType(MetricType::COSINE));
        checked(args.SetExpression(req.at("filter").get<std::string>()));
        checked(args.SetGuaranteeTimestamp(GuaranteeStrongTs()));
        for (const auto& field : req.at("output_fields")) checked(args.AddOutputField(field.get<std::string>()));
        checked(args.AddTargetVector("embedding", req.at("vector").get<std::vector<float>>()));
        SearchResults result;
        checked(ctx.db->Search(args, result, req.value("timeout_ms", 5000)));
        Json hits = Json::array();
        for (const auto& batch : result.Results()) {
            auto rows = rows_from(batch.OutputFields());
            if (rows.size() != batch.Scores().size()) throw std::runtime_error("SDK score/row mismatch");
            Json single = Json::array();
            for (size_t i = 0; i < rows.size(); ++i)
                single.push_back({{"entity", rows[i]}, {"distance", batch.Scores()[i]}});
            hits.push_back(std::move(single));
        }
        return hits;
    }
    throw std::invalid_argument("Unknown operation");
}
} // namespace

extern "C" {
int mokoa_vector_abi_version() { return 1; }
void* mokoa_vector_create() {
    try { return new Context(); } catch (...) { return nullptr; }
}
char* mokoa_vector_call(void* handle, const char* request) {
    try {
        Json response;
        try {
            if (!handle || !request) throw std::invalid_argument("Null handle/request");
            auto& ctx = *static_cast<Context*>(handle);
            std::lock_guard<std::mutex> lock(ctx.mutex);
            response = {{"ok", true}, {"result", dispatch(ctx, Json::parse(request))}};
        } catch (const SdkError& exc) {
            response = {{"ok", false}, {"code", exc.code}, {"error", exc.what()}};
        } catch (const std::exception& exc) {
            response = {{"ok", false}, {"code", -1}, {"error", exc.what()}};
        } catch (...) {
            response = {{"ok", false}, {"code", -1}, {"error", "Unknown native exception"}};
        }
        auto text = response.dump();
        auto* buffer = static_cast<char*>(std::malloc(text.size() + 1));
        if (buffer) std::memcpy(buffer, text.c_str(), text.size() + 1);
        return buffer;
    } catch (...) { return nullptr; }
}
void mokoa_vector_free(void* response) { std::free(response); }
void mokoa_vector_destroy(void* handle) {
    auto* ctx = static_cast<Context*>(handle);
    if (!ctx) return;
    try { if (ctx->db) ctx->db->Disconnect(); } catch (...) {}
    delete ctx;
}
}
