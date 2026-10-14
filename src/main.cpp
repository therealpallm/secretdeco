#include <Geode/Geode.hpp>
#include <Geode/modify/EditorUI.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>
#include <Geode/binding/UndoObject.hpp>
#include <Geode/ui/Popup.hpp>
#include <Geode/ui/TextInput.hpp>
#include <Geode/utils/async.hpp>
#include <Geode/utils/web.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <vector>

using namespace geode::prelude;

namespace {
constexpr size_t kLimit = 64;
constexpr float kCoordLimit = 100000.f;
struct Block { float x; float y; };

bool coordinate(TextInput* input, double& value) {
    auto text = std::string(input->getString().c_str());
    try {
        size_t used = 0;
        value = std::stod(text, &used);
        return used == text.size() && std::isfinite(value) && std::abs(value) <= kCoordLimit;
    } catch (...) {
        return false;
    }
}

class AIPopup : public Popup {
    WeakRef<EditorUI> m_editor;
    TextInput* m_prompt = nullptr;
    TextInput* m_x = nullptr;
    TextInput* m_y = nullptr;
    CCLabelBMFont* m_status = nullptr;
    CCDrawNode* m_preview = nullptr;
    CCMenuItemSpriteExtra* m_generate = nullptr;
    CCMenuItemSpriteExtra* m_apply = nullptr;
    async::TaskHolder<web::WebResponse> m_request;
    std::vector<Block> m_blocks;
    std::string m_snapshot;

    void status(std::string const& text) {
        m_status->setString(text.c_str());
    }

    Ref<EditorUI> currentEditor() {
        auto editor = m_editor.lock();
        if (!editor || EditorUI::get() != editor.data() ||
            LevelEditorLayer::get() != editor->m_editorLayer)
            return {};
        return editor;
    }

    void clearPlan() {
        m_blocks.clear();
        m_preview->clear();
        m_apply->setEnabled(false);
        m_apply->setOpacity(100);
    }

    void drawPreview() {
        m_preview->clear();
        if (m_blocks.empty()) return;
        float minX = m_blocks[0].x - 15.f, maxX = m_blocks[0].x + 15.f;
        float minY = m_blocks[0].y - 15.f, maxY = m_blocks[0].y + 15.f;
        for (auto const& b : m_blocks) {
            minX = std::min(minX, b.x - 15.f); maxX = std::max(maxX, b.x + 15.f);
            minY = std::min(minY, b.y - 15.f); maxY = std::max(maxY, b.y + 15.f);
        }
        float scale = std::min(270.f / (maxX - minX), 60.f / (maxY - minY));
        float startX = (320.f - (maxX - minX) * scale) / 2.f;
        float startY = 63.f + (60.f - (maxY - minY) * scale) / 2.f;
        for (auto const& b : m_blocks) {
            CCPoint lower{startX + (b.x - 15.f - minX) * scale,
                          startY + (b.y - 15.f - minY) * scale};
            CCPoint upper{lower.x + 30.f * scale, lower.y + 30.f * scale};
            m_preview->drawRect(lower, upper, {0.40f, 0.22f, 0.65f, 0.9f},
                                0.5f, {0.82f, 0.67f, 1.f, 1.f});
        }
    }

    void onGenerate(CCObject*) {
        if (m_request.isPending()) return;
        clearPlan();
        auto editor = currentEditor();
        if (!editor) { status("Reopen the editor and try again."); return; }
        auto prompt = std::string(m_prompt->getString().c_str());
        if (prompt.empty()) { status("Enter a request first."); return; }
        auto token = Mod::get()->getSettingValue<std::string>("helper-token");
        if (token.empty()) { status("Paste the helper token into this mod's settings."); return; }
        double x = 0, y = 0;
        if (!coordinate(m_x, x) || !coordinate(m_y, y)) {
            status("X and Y must be numbers within +/-100000."); return;
        }
        auto selected = matjson::Value::array();
        auto objects = editor->getSelectedObjects();
        if (objects && objects->count() > kLimit) {
            status("Select at most 64 objects for this prototype."); return;
        }
        if (objects) {
            for (auto object : CCArrayExt<GameObject*>(objects)) {
                selected.push(matjson::makeObject({
                    {"id", object->m_objectID},
                    {"x", object->getPositionX()}, {"y", object->getPositionY()}
                }));
            }
        }
        auto payload = matjson::makeObject({
            {"prompt", prompt}, {"origin", matjson::makeObject({{"x", x}, {"y", y}})},
            {"selected", selected}
        });
        m_snapshot = std::string(editor->m_editorLayer->getLevelString().c_str());
        status("Generating a plan...");
        m_generate->setEnabled(false);
        m_request.spawn(
            web::WebRequest().timeout(std::chrono::seconds(75))
                .header("Authorization", "Bearer " + token)
                .bodyJSON(payload).post("http://127.0.0.1:8765/plan"),
            [this](web::WebResponse response) {
                m_generate->setEnabled(true);
                auto json = response.json();
                if (!response.ok()) {
                    status(json ? json.unwrap()["error"].asString().unwrapOr("Helper error.")
                                : "Start the helper and check the pairing token.");
                    return;
                }
                if (!json) { status("The helper returned invalid JSON."); return; }
                auto value = json.unwrap();
                if (!value.isObject()) { status("The helper returned an invalid plan."); return; }
                auto array = value["objects"].asArray();
                auto summary = value["summary"].asString();
                if (!array || !summary || array.unwrap().size() > kLimit ||
                    summary.unwrap().size() > 300) {
                    status("The helper returned an invalid block plan."); return;
                }
                std::vector<Block> blocks;
                for (auto const& obj : array.unwrap()) {
                    if (!obj.isObject()) { status("Rejected an invalid object entry."); return; }
                    auto id = obj["id"].asInt();
                    auto x = obj["x"].asDouble(), y = obj["y"].asDouble();
                    if (!id || id.unwrap() != 1 || !x || !y ||
                        !std::isfinite(x.unwrap()) || !std::isfinite(y.unwrap()) ||
                        std::abs(x.unwrap()) > kCoordLimit || std::abs(y.unwrap()) > kCoordLimit) {
                        status("Rejected an unsupported object or coordinate."); return;
                    }
                    Block block{static_cast<float>(x.unwrap()), static_cast<float>(y.unwrap())};
                    if (std::any_of(blocks.begin(), blocks.end(), [block](auto const& b) {
                        return b.x == block.x && b.y == block.y;
                    })) { status("Rejected duplicate block locations."); return; }
                    blocks.push_back(block);
                }
                if (!currentEditor()) { status("The editor was closed. Reopen AI."); return; }
                if (blocks.empty()) { status(summary.unwrap()); return; }
                m_blocks = std::move(blocks);
                status(fmt::format("{} blocks. First center: ({:.1f}, {:.1f}).\nApply to add them; close to cancel.",
                                  m_blocks.size(), m_blocks[0].x, m_blocks[0].y));
                drawPreview();
                m_apply->setEnabled(true);
                m_apply->setOpacity(255);
            }
        );
    }

    void onApply(CCObject*) {
        auto editor = currentEditor();
        if (!editor || m_blocks.empty()) return;
        auto layer = editor->m_editorLayer;
        if (m_snapshot != std::string(layer->getLevelString().c_str())) {
            clearPlan(); status("The level changed. Generate a fresh plan."); return;
        }
        auto created = CCArray::create();
        for (auto const& b : m_blocks) {
            // true = do not create per-object undo entries. One batch is added below.
            auto object = layer->createObject(1, {b.x, b.y}, true);
            if (!object) {
                for (auto previous : CCArrayExt<GameObject*>(created))
                    layer->removeObject(previous, true);
                clearPlan(); status("Could not create a block. No additions kept."); return;
            }
            created->addObject(object);
        }
        auto undo = UndoObject::createWithArray(created, UndoCommand::Paste);
        if (!undo) {
            for (auto previous : CCArrayExt<GameObject*>(created))
                layer->removeObject(previous, true);
            clearPlan(); status("Could not register undo. No additions kept."); return;
        }
        layer->addToUndoList(undo, false);
        editor->deselectAll();
        editor->selectObjects(created, true);
        editor->updateButtons();
        auto count = m_blocks.size();
        clearPlan();
        status(fmt::format("Added {} blocks. Use the editor's Undo to remove them.", count));
    }

    void onClose(CCObject* sender) override {
        m_request.cancel();
        Popup::onClose(sender);
    }

    bool init(EditorUI* editor) {
        if (!Popup::init(320.f, 300.f)) return false;
        m_editor = editor;
        setTitle("Drew Editor AI");
        auto hint = CCLabelBMFont::create("Basic blocks only - preview before applying", "bigFont.fnt");
        hint->setScale(.32f); hint->setPosition({160.f, 252.f}); m_mainLayer->addChild(hint);
        m_prompt = TextInput::create(285.f, "make 10 blocks in a row");
        m_prompt->setCommonFilter(CommonFilter::Any);
        m_prompt->setMaxCharCount(500); m_prompt->setPosition({160.f, 216.f});
        m_mainLayer->addChild(m_prompt);
        m_x = TextInput::create(125.f, "X center");
        m_y = TextInput::create(125.f, "Y center");
        for (auto input : {m_x, m_y}) { input->setCommonFilter(CommonFilter::Float); input->setMaxCharCount(12); }
        m_x->setLabel("First block X"); m_y->setLabel("First block Y");
        m_x->setString("315"); m_y->setString("105");
        m_x->setPosition({88.f, 167.f}); m_y->setPosition({232.f, 167.f});
        m_mainLayer->addChild(m_x); m_mainLayer->addChild(m_y);
        m_preview = CCDrawNode::create(); m_mainLayer->addChild(m_preview);
        m_status = CCLabelBMFont::create("Start the helper, then paste its token in mod settings.", "bigFont.fnt");
        m_status->setScale(.32f); m_status->setWidth(875.f);
        m_status->setAlignment(kCCTextAlignmentCenter); m_status->setPosition({160.f, 48.f});
        m_mainLayer->addChild(m_status);
        auto menu = CCMenu::create(); menu->setPosition({0.f, 0.f}); m_mainLayer->addChild(menu);
        m_generate = CCMenuItemSpriteExtra::create(ButtonSprite::create("Generate"), this, menu_selector(AIPopup::onGenerate));
        m_apply = CCMenuItemSpriteExtra::create(ButtonSprite::create("Apply"), this, menu_selector(AIPopup::onApply));
        m_generate->setScale(.65f); m_generate->setPosition({99.f, 22.f}); menu->addChild(m_generate);
        m_apply->setScale(.65f); m_apply->setPosition({226.f, 22.f}); menu->addChild(m_apply);
        clearPlan();
        return true;
    }

public:
    static AIPopup* create(EditorUI* editor) {
        auto popup = new AIPopup();
        if (popup->init(editor)) { popup->autorelease(); return popup; }
        delete popup; return nullptr;
    }
};
}

class $modify(DrewAIEditorUI, EditorUI) {
    static void onModify(auto& self) {
        (void)self.setHookPriorityAfterPost("EditorUI::init", "geode.node-ids");
    }

    bool init(LevelEditorLayer* layer) {
        if (!EditorUI::init(layer)) return false;
        auto menu = getChildByID("settings-menu");
        if (!menu) {
            log::warn("Drew Editor AI could not find settings-menu. Check Node IDs.");
            return true;
        }
        auto button = CCMenuItemSpriteExtra::create(ButtonSprite::create("AI"), this,
                                                    menu_selector(DrewAIEditorUI::onAI));
        button->setID("ai-button"_spr); button->setScale(.55f);
        menu->addChild(button); menu->updateLayout();
        return true;
    }

    void onAI(CCObject*) {
        if (auto popup = AIPopup::create(this)) popup->show();
    }
};
