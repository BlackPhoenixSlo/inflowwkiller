"use client";

/**
 * EmojiBar — quick-row + picker popover with search + recents.
 *
 * Insertion preserves the textarea's cursor so the user can type, drop
 * an emoji, keep typing. The full emoji set lives in EMOJI_CATEGORIES;
 * the picker shows them tabbed by category with a search box that
 * matches by keyword.
 *
 * Recents persist in localStorage so the picker remembers what you used
 * across reloads.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { cn } from "@/lib/utils";

export const QUICK_EMOJIS = ["❤️", "😍", "🔥", "👍", "😂", "💀", "✨", "🎉"] as const;

const LS_RECENTS_KEY = "chatterly:emoji_recents";
const MAX_RECENTS = 24;
/** Cross-instance "recents changed" pulse so every mounted EmojiQuickRow
 *  / picker re-reads from localStorage after a pick. The native `storage`
 *  event only fires on OTHER tabs, not within the same tab — so we
 *  dispatch our own `CustomEvent` on `window`. */
const RECENTS_EVENT = "chatterly:emoji_recents:changed";

/**
 * Each entry: [emoji, keywords]. Keywords drive search and let one emoji
 * surface under multiple terms (e.g. 🍆 matches "eggplant" AND "cock").
 */
type EmojiEntry = readonly [string, readonly string[]];

const FLIRTY: readonly EmojiEntry[] = [
  ["❤️", ["heart", "love", "red"]],
  ["🧡", ["orange", "heart"]],
  ["💛", ["yellow", "heart"]],
  ["💚", ["green", "heart"]],
  ["💙", ["blue", "heart"]],
  ["💜", ["purple", "heart"]],
  ["🖤", ["black", "heart"]],
  ["🤍", ["white", "heart"]],
  ["🤎", ["brown", "heart"]],
  ["💕", ["hearts", "love", "pink"]],
  ["💖", ["sparkling", "heart", "pink"]],
  ["💗", ["growing", "heart"]],
  ["💓", ["beating", "heart"]],
  ["💘", ["arrow", "heart", "cupid"]],
  ["💝", ["gift", "heart"]],
  ["💞", ["revolving", "hearts"]],
  ["💟", ["heart", "decoration"]],
  ["❣️", ["heart", "exclamation"]],
  ["💔", ["broken", "heart"]],
  ["♥️", ["heart", "suit"]],
  ["💌", ["love", "letter"]],
  ["😍", ["love", "eyes", "heart"]],
  ["🥰", ["smile", "hearts", "love"]],
  ["😘", ["kiss", "blow"]],
  ["😚", ["kiss", "closed"]],
  ["😙", ["kiss", "smile"]],
  ["😗", ["kiss"]],
  ["💋", ["kiss", "lips", "mark"]],
  ["👄", ["mouth", "lips"]],
  ["🫦", ["bite", "lip"]],
  ["👅", ["tongue"]],
  ["🥵", ["hot", "sweat", "horny"]],
  ["🤤", ["drool", "wet"]],
  ["😏", ["smirk", "tease"]],
  ["😉", ["wink", "flirt"]],
  ["😈", ["devil", "naughty", "horny"]],
  ["👿", ["angry", "devil"]],
  ["💦", ["drop", "wet", "splash", "cum"]],
  ["💧", ["droplet", "water"]],
  ["🔥", ["fire", "hot", "lit"]],
  ["💯", ["100", "hundred"]],
  ["✨", ["sparkle", "shine"]],
  ["💫", ["dizzy", "star"]],
  ["⭐", ["star"]],
  ["🌟", ["glow", "star"]],
  ["🌹", ["rose", "flower"]],
  ["🌷", ["tulip", "flower"]],
  ["🌸", ["cherry", "blossom"]],
  ["🌺", ["hibiscus", "flower"]],
  ["💐", ["bouquet", "flowers"]],
  ["🌶️", ["spicy", "pepper", "hot"]],
  ["🍑", ["peach", "butt", "ass"]],
  ["🍆", ["eggplant", "cock", "dick"]],
  ["🍒", ["cherry", "boob"]],
  ["🍌", ["banana", "cock"]],
  ["🥒", ["cucumber"]],
  ["🌽", ["corn"]],
  ["🍯", ["honey"]],
  ["🍭", ["lollipop", "lick"]],
  ["🍦", ["icecream", "soft"]],
  ["🍓", ["strawberry"]],
  ["🍫", ["chocolate", "sweet"]],
  ["🥥", ["coconut", "boob"]],
  ["🫐", ["blueberry"]],
  ["🍇", ["grapes"]],
  ["🥑", ["avocado"]],
];

const FACES: readonly EmojiEntry[] = [
  ["😊", ["smile", "blush"]],
  ["😀", ["smile", "happy"]],
  ["😃", ["smile", "open", "happy"]],
  ["😄", ["smile", "eyes"]],
  ["😁", ["grin"]],
  ["😆", ["laugh"]],
  ["😅", ["sweat", "smile"]],
  ["🤣", ["rofl", "laugh"]],
  ["😂", ["laugh", "tears", "lol"]],
  ["🙂", ["smile", "slight"]],
  ["🙃", ["upside", "down"]],
  ["🫠", ["melting"]],
  ["😉", ["wink"]],
  ["😌", ["relieved"]],
  ["😍", ["heart", "eyes"]],
  ["🥰", ["love", "smile"]],
  ["😘", ["kiss"]],
  ["😗", ["kiss"]],
  ["😙", ["kiss", "smile"]],
  ["😚", ["kiss", "closed"]],
  ["😋", ["yum", "tasty"]],
  ["😛", ["tongue"]],
  ["😝", ["tongue", "wink"]],
  ["😜", ["tongue", "wink"]],
  ["🤪", ["zany", "crazy"]],
  ["🤨", ["raised", "eyebrow"]],
  ["🧐", ["monocle", "thinking"]],
  ["🤓", ["nerd"]],
  ["😎", ["cool", "sunglasses"]],
  ["🥸", ["disguise"]],
  ["🤩", ["star", "eyes", "excited"]],
  ["🥳", ["party"]],
  ["😏", ["smirk", "smug"]],
  ["😒", ["unamused"]],
  ["😞", ["disappointed"]],
  ["😔", ["pensive"]],
  ["😟", ["worried"]],
  ["😕", ["confused"]],
  ["🙁", ["frown", "slight"]],
  ["☹️", ["frown"]],
  ["😣", ["persevere"]],
  ["😖", ["confounded"]],
  ["😫", ["tired"]],
  ["😩", ["weary"]],
  ["🥺", ["pleading", "puppy"]],
  ["😢", ["cry", "sad"]],
  ["😭", ["sob", "crying"]],
  ["😤", ["triumph", "huff"]],
  ["😠", ["angry"]],
  ["😡", ["angry", "mad"]],
  ["🤬", ["swear", "cuss"]],
  ["🤯", ["mind", "blown"]],
  ["😳", ["flushed", "shock"]],
  ["🥵", ["hot"]],
  ["🥶", ["cold"]],
  ["😱", ["scream", "shock"]],
  ["😨", ["fearful"]],
  ["😰", ["anxious"]],
  ["😥", ["sad", "relieved"]],
  ["😓", ["downcast", "sweat"]],
  ["🤗", ["hug"]],
  ["🤔", ["think", "thinking"]],
  ["🫣", ["peeking"]],
  ["🤭", ["giggle"]],
  ["🤫", ["shush", "shh"]],
  ["🤥", ["lying", "pinocchio"]],
  ["😶", ["no", "mouth"]],
  ["😐", ["neutral"]],
  ["😑", ["expressionless"]],
  ["😬", ["grimace"]],
  ["🙄", ["eyeroll"]],
  ["😯", ["surprised"]],
  ["😦", ["frown", "open"]],
  ["😧", ["anguished"]],
  ["😮", ["open", "mouth", "wow"]],
  ["😲", ["astonished"]],
  ["🥱", ["yawn", "bored"]],
  ["😴", ["sleep"]],
  ["🤤", ["drool"]],
  ["😪", ["sleepy"]],
  ["😵", ["dizzy", "ko"]],
  ["😵‍💫", ["dizzy", "spiral"]],
  ["🤐", ["zipper"]],
  ["🥴", ["woozy"]],
  ["🤢", ["nauseated"]],
  ["🤮", ["vomit"]],
  ["🤧", ["sneeze", "sick"]],
  ["😷", ["mask", "sick"]],
  ["🤒", ["thermometer", "sick"]],
  ["🤕", ["bandage", "hurt"]],
  ["🤑", ["money", "mouth"]],
  ["🤠", ["cowboy"]],
  ["😇", ["angel"]],
  ["🤡", ["clown"]],
  ["🥹", ["holding", "back", "tears"]],
  ["😺", ["cat", "smile"]],
  ["😸", ["cat", "grin"]],
  ["😹", ["cat", "tears"]],
  ["😻", ["cat", "heart"]],
  ["😼", ["cat", "smirk"]],
  ["😽", ["cat", "kiss"]],
  ["🙀", ["cat", "scream"]],
  ["😿", ["cat", "cry"]],
  ["😾", ["cat", "pout"]],
  ["🙈", ["see", "no", "evil", "shy"]],
  ["🙉", ["hear", "no"]],
  ["🙊", ["speak", "no"]],
];

const GESTURES: readonly EmojiEntry[] = [
  ["👍", ["thumbs", "up", "yes"]],
  ["👎", ["thumbs", "down", "no"]],
  ["👏", ["clap"]],
  ["🙌", ["raised", "hands", "praise"]],
  ["🙏", ["pray", "thanks", "please"]],
  ["🤝", ["handshake", "deal"]],
  ["✌️", ["peace"]],
  ["🤞", ["fingers", "crossed", "luck"]],
  ["🤟", ["love", "you", "rock"]],
  ["🤘", ["rock", "horns"]],
  ["👌", ["ok"]],
  ["🤌", ["pinch", "italian"]],
  ["🤏", ["pinching", "small"]],
  ["✋", ["raised", "hand", "stop"]],
  ["🤚", ["raised", "back"]],
  ["🖐️", ["splayed", "hand"]],
  ["🖖", ["vulcan", "spock"]],
  ["👋", ["wave", "hello", "hi", "bye"]],
  ["🫶", ["heart", "hands"]],
  ["🫰", ["love", "money"]],
  ["🫳", ["palm", "down"]],
  ["🫴", ["palm", "up"]],
  ["🫵", ["pointing", "you"]],
  ["☝️", ["index", "up"]],
  ["👆", ["point", "up"]],
  ["👇", ["point", "down"]],
  ["👈", ["point", "left"]],
  ["👉", ["point", "right"]],
  ["👊", ["fist", "bump"]],
  ["✊", ["raised", "fist"]],
  ["🤛", ["fist", "left"]],
  ["🤜", ["fist", "right"]],
  ["🤲", ["palms", "up"]],
  ["🤙", ["call", "me", "shaka"]],
  ["💅", ["nail", "polish"]],
  ["🤳", ["selfie"]],
  ["👀", ["eyes", "looking"]],
  ["👁️", ["eye"]],
  ["🧠", ["brain"]],
  ["💪", ["muscle", "strong"]],
  ["🦾", ["mechanical", "arm"]],
  ["🦿", ["mechanical", "leg"]],
  ["🦵", ["leg"]],
  ["🦶", ["foot"]],
  ["🫀", ["heart", "anatomical"]],
  ["🫁", ["lungs"]],
  ["🦷", ["tooth"]],
  ["🦴", ["bone"]],
];

const PARTY: readonly EmojiEntry[] = [
  ["🎉", ["party", "celebrate", "tada"]],
  ["🎊", ["confetti"]],
  ["🥂", ["cheers", "toast"]],
  ["🍾", ["champagne"]],
  ["🍷", ["wine"]],
  ["🍸", ["cocktail", "martini"]],
  ["🍹", ["tropical", "drink"]],
  ["🍺", ["beer"]],
  ["🍻", ["beers", "cheers"]],
  ["🥃", ["whiskey", "tumbler"]],
  ["🍶", ["sake"]],
  ["🧉", ["mate"]],
  ["🥤", ["cup", "straw"]],
  ["🧋", ["bubble", "tea"]],
  ["☕", ["coffee", "hot"]],
  ["🍵", ["tea", "green"]],
  ["🍼", ["bottle", "baby"]],
  ["🎁", ["gift", "present"]],
  ["🎀", ["ribbon", "bow"]],
  ["💎", ["diamond", "gem"]],
  ["💍", ["ring"]],
  ["💰", ["money", "bag"]],
  ["💵", ["dollar", "money"]],
  ["💴", ["yen"]],
  ["💶", ["euro"]],
  ["💷", ["pound"]],
  ["🪙", ["coin"]],
  ["💸", ["cash", "flying"]],
  ["💳", ["card", "credit"]],
  ["🧧", ["red", "envelope"]],
  ["🎂", ["cake", "birthday"]],
  ["🍰", ["cake", "slice"]],
  ["🧁", ["cupcake"]],
  ["🍩", ["donut"]],
  ["🍪", ["cookie"]],
  ["🎈", ["balloon"]],
  ["🪅", ["pinata"]],
  ["🎆", ["fireworks"]],
  ["🎇", ["sparkler"]],
  ["🪩", ["mirror", "ball", "disco"]],
  ["⭐", ["star"]],
  ["🌟", ["sparkle", "star"]],
  ["💫", ["dizzy", "star"]],
  ["⚡", ["zap", "lightning"]],
  ["💥", ["boom", "collision"]],
  ["💀", ["skull", "dead", "lol"]],
  ["☠️", ["skull", "crossbones"]],
  ["👻", ["ghost"]],
  ["👽", ["alien"]],
  ["🤖", ["robot"]],
];

const ANIMALS: readonly EmojiEntry[] = [
  ["🐶", ["dog", "puppy"]],
  ["🐱", ["cat", "kitten"]],
  ["🐭", ["mouse"]],
  ["🐹", ["hamster"]],
  ["🐰", ["rabbit", "bunny"]],
  ["🦊", ["fox"]],
  ["🐻", ["bear"]],
  ["🐼", ["panda"]],
  ["🐻‍❄️", ["polar", "bear"]],
  ["🐨", ["koala"]],
  ["🐯", ["tiger"]],
  ["🦁", ["lion"]],
  ["🐮", ["cow"]],
  ["🐷", ["pig"]],
  ["🐸", ["frog"]],
  ["🐵", ["monkey"]],
  ["🙈", ["see", "no", "evil"]],
  ["🙉", ["hear", "no"]],
  ["🙊", ["speak", "no"]],
  ["🐒", ["monkey"]],
  ["🐔", ["chicken"]],
  ["🐧", ["penguin"]],
  ["🐦", ["bird"]],
  ["🐤", ["chick", "baby"]],
  ["🦆", ["duck"]],
  ["🦅", ["eagle"]],
  ["🦉", ["owl"]],
  ["🦇", ["bat"]],
  ["🐺", ["wolf"]],
  ["🐗", ["boar"]],
  ["🐴", ["horse"]],
  ["🦄", ["unicorn"]],
  ["🐝", ["bee"]],
  ["🐛", ["bug", "worm"]],
  ["🦋", ["butterfly"]],
  ["🐌", ["snail"]],
  ["🐞", ["ladybug"]],
  ["🐢", ["turtle"]],
  ["🐍", ["snake"]],
  ["🦎", ["lizard"]],
  ["🐙", ["octopus"]],
  ["🦑", ["squid"]],
  ["🦐", ["shrimp"]],
  ["🦀", ["crab"]],
  ["🐡", ["pufferfish"]],
  ["🐠", ["fish", "tropical"]],
  ["🐟", ["fish"]],
  ["🐬", ["dolphin"]],
  ["🐳", ["whale"]],
  ["🦈", ["shark"]],
  ["🐊", ["crocodile"]],
  ["🐅", ["tiger"]],
  ["🐆", ["leopard"]],
  ["🦓", ["zebra"]],
  ["🦒", ["giraffe"]],
  ["🐘", ["elephant"]],
  ["🦏", ["rhino"]],
  ["🦛", ["hippo"]],
  ["🐪", ["camel"]],
  ["🦘", ["kangaroo"]],
];

const FOOD: readonly EmojiEntry[] = [
  ["🍎", ["apple", "red"]],
  ["🍏", ["apple", "green"]],
  ["🍊", ["orange"]],
  ["🍋", ["lemon"]],
  ["🍌", ["banana"]],
  ["🍉", ["watermelon"]],
  ["🍇", ["grapes"]],
  ["🍓", ["strawberry"]],
  ["🫐", ["blueberry"]],
  ["🍈", ["melon"]],
  ["🍒", ["cherry"]],
  ["🍑", ["peach"]],
  ["🥭", ["mango"]],
  ["🍍", ["pineapple"]],
  ["🥥", ["coconut"]],
  ["🥝", ["kiwi"]],
  ["🍅", ["tomato"]],
  ["🍆", ["eggplant"]],
  ["🥑", ["avocado"]],
  ["🥦", ["broccoli"]],
  ["🥬", ["leafy", "green"]],
  ["🥒", ["cucumber"]],
  ["🌶️", ["pepper", "hot"]],
  ["🫑", ["bell", "pepper"]],
  ["🌽", ["corn"]],
  ["🥕", ["carrot"]],
  ["🫒", ["olive"]],
  ["🧄", ["garlic"]],
  ["🧅", ["onion"]],
  ["🥔", ["potato"]],
  ["🍠", ["sweet", "potato"]],
  ["🥐", ["croissant"]],
  ["🥯", ["bagel"]],
  ["🍞", ["bread"]],
  ["🥖", ["baguette"]],
  ["🥨", ["pretzel"]],
  ["🧇", ["waffle"]],
  ["🥞", ["pancakes"]],
  ["🧀", ["cheese"]],
  ["🍖", ["meat"]],
  ["🍗", ["chicken", "leg"]],
  ["🥩", ["steak"]],
  ["🥓", ["bacon"]],
  ["🍔", ["burger"]],
  ["🍟", ["fries"]],
  ["🍕", ["pizza"]],
  ["🌭", ["hotdog"]],
  ["🥪", ["sandwich"]],
  ["🌮", ["taco"]],
  ["🌯", ["burrito"]],
  ["🥙", ["pita", "wrap"]],
  ["🧆", ["falafel"]],
  ["🥚", ["egg"]],
  ["🍳", ["egg", "fried"]],
  ["🥘", ["paella", "pan"]],
  ["🍲", ["stew"]],
  ["🫕", ["fondue"]],
  ["🥣", ["bowl", "spoon"]],
  ["🥗", ["salad"]],
  ["🍿", ["popcorn"]],
  ["🧈", ["butter"]],
  ["🧂", ["salt"]],
  ["🥫", ["canned"]],
  ["🍱", ["bento"]],
  ["🍘", ["rice", "cracker"]],
  ["🍙", ["rice", "ball"]],
  ["🍚", ["rice"]],
  ["🍛", ["curry"]],
  ["🍜", ["ramen", "noodles"]],
  ["🍝", ["spaghetti", "pasta"]],
  ["🍠", ["roasted", "sweet"]],
  ["🍢", ["oden"]],
  ["🍣", ["sushi"]],
  ["🍤", ["shrimp", "fry"]],
  ["🍥", ["fish", "cake"]],
  ["🥮", ["mooncake"]],
  ["🍡", ["dango"]],
  ["🥟", ["dumpling"]],
  ["🥠", ["fortune", "cookie"]],
  ["🥡", ["takeout"]],
  ["🍦", ["icecream", "soft"]],
  ["🍧", ["shaved", "ice"]],
  ["🍨", ["icecream"]],
  ["🍩", ["donut"]],
  ["🍪", ["cookie"]],
  ["🎂", ["birthday", "cake"]],
  ["🍰", ["cake"]],
  ["🧁", ["cupcake"]],
  ["🥧", ["pie"]],
  ["🍫", ["chocolate"]],
  ["🍬", ["candy"]],
  ["🍭", ["lollipop"]],
  ["🍮", ["custard"]],
  ["🍯", ["honey"]],
];

const ACTIVITIES: readonly EmojiEntry[] = [
  ["⚽", ["soccer", "ball"]],
  ["🏀", ["basketball"]],
  ["🏈", ["football", "american"]],
  ["⚾", ["baseball"]],
  ["🥎", ["softball"]],
  ["🎾", ["tennis"]],
  ["🏐", ["volleyball"]],
  ["🏉", ["rugby"]],
  ["🥏", ["frisbee"]],
  ["🎱", ["pool", "8ball"]],
  ["🪀", ["yoyo"]],
  ["🏓", ["pingpong"]],
  ["🏸", ["badminton"]],
  ["🥊", ["boxing"]],
  ["🥋", ["martial", "arts"]],
  ["⛳", ["golf"]],
  ["⛸️", ["skate"]],
  ["🎣", ["fishing"]],
  ["🤿", ["diving"]],
  ["🎽", ["running", "shirt"]],
  ["🛹", ["skateboard"]],
  ["🛼", ["roller", "skate"]],
  ["🛷", ["sled"]],
  ["⛷️", ["ski"]],
  ["🏂", ["snowboard"]],
  ["🪂", ["parachute"]],
  ["🏋️", ["lifting"]],
  ["🤸", ["cartwheel"]],
  ["🤺", ["fencing"]],
  ["🤾", ["handball"]],
  ["🏌️", ["golfer"]],
  ["🏇", ["horse", "racing"]],
  ["🧘", ["yoga", "meditation"]],
  ["🏄", ["surfing"]],
  ["🏊", ["swimming"]],
  ["🚣", ["rowing"]],
  ["🧗", ["climbing"]],
  ["🚴", ["biking"]],
  ["🚵", ["mtb"]],
  ["🎨", ["art", "palette"]],
  ["🎬", ["movie", "clapper"]],
  ["🎤", ["mic", "sing"]],
  ["🎧", ["headphones", "music"]],
  ["🎼", ["sheet", "music"]],
  ["🎵", ["note"]],
  ["🎶", ["notes"]],
  ["🎹", ["piano"]],
  ["🥁", ["drum"]],
  ["🎷", ["sax"]],
  ["🎺", ["trumpet"]],
  ["🎸", ["guitar"]],
  ["🪕", ["banjo"]],
  ["🎻", ["violin"]],
  ["🎲", ["dice", "game"]],
  ["♟️", ["chess", "pawn"]],
  ["🎯", ["dart", "bullseye"]],
  ["🎳", ["bowling"]],
  ["🎮", ["gamepad"]],
  ["🎰", ["slot", "machine"]],
  ["🧩", ["puzzle"]],
];

const TRAVEL: readonly EmojiEntry[] = [
  ["🚗", ["car"]],
  ["🚕", ["taxi"]],
  ["🚙", ["suv"]],
  ["🚌", ["bus"]],
  ["🚎", ["trolley"]],
  ["🏎️", ["race", "car"]],
  ["🚓", ["police", "car"]],
  ["🚑", ["ambulance"]],
  ["🚒", ["fire", "engine"]],
  ["🚐", ["minibus"]],
  ["🛻", ["pickup"]],
  ["🚚", ["truck"]],
  ["🚛", ["semi"]],
  ["🚜", ["tractor"]],
  ["🛵", ["scooter"]],
  ["🏍️", ["motorcycle"]],
  ["🚲", ["bike"]],
  ["🛴", ["kick", "scooter"]],
  ["🚂", ["train"]],
  ["🚆", ["train"]],
  ["🚊", ["tram"]],
  ["🚉", ["station"]],
  ["✈️", ["plane"]],
  ["🛫", ["takeoff"]],
  ["🛬", ["landing"]],
  ["🛩️", ["small", "plane"]],
  ["🚀", ["rocket"]],
  ["🛸", ["ufo"]],
  ["🚁", ["helicopter"]],
  ["⛵", ["sailboat"]],
  ["🛶", ["canoe"]],
  ["🚤", ["speedboat"]],
  ["🛥️", ["motorboat"]],
  ["🛳️", ["ship", "passenger"]],
  ["⛴️", ["ferry"]],
  ["🚢", ["ship"]],
  ["⚓", ["anchor"]],
  ["🏖️", ["beach"]],
  ["🏝️", ["island"]],
  ["🏜️", ["desert"]],
  ["🌋", ["volcano"]],
  ["⛰️", ["mountain"]],
  ["🏔️", ["mountain", "snow"]],
  ["🗻", ["fuji"]],
  ["🏕️", ["camping"]],
  ["🛖", ["hut"]],
  ["🏠", ["house"]],
  ["🏡", ["house", "garden"]],
  ["🏢", ["office"]],
  ["🏨", ["hotel"]],
  ["🏫", ["school"]],
  ["🏥", ["hospital"]],
  ["🗽", ["statue", "liberty"]],
  ["🗼", ["tokyo", "tower"]],
  ["🗿", ["moai"]],
  ["🏰", ["castle"]],
  ["⛩️", ["shrine"]],
  ["🕌", ["mosque"]],
  ["⛪", ["church"]],
  ["🕍", ["synagogue"]],
  ["🛕", ["temple"]],
  ["🌍", ["earth", "africa"]],
  ["🌎", ["earth", "americas"]],
  ["🌏", ["earth", "asia"]],
  ["🌐", ["globe"]],
  ["🗺️", ["map"]],
  ["☀️", ["sun"]],
  ["🌤️", ["sun", "cloud"]],
  ["⛅", ["partly", "cloudy"]],
  ["🌥️", ["cloud", "sun"]],
  ["☁️", ["cloud"]],
  ["🌧️", ["rain"]],
  ["⛈️", ["thunderstorm"]],
  ["🌩️", ["lightning"]],
  ["🌨️", ["snow"]],
  ["❄️", ["snowflake"]],
  ["☃️", ["snowman"]],
  ["⛄", ["snowman", "no", "snow"]],
  ["🌬️", ["wind"]],
  ["🌪️", ["tornado"]],
  ["🌈", ["rainbow"]],
  ["🌙", ["moon"]],
  ["🌛", ["moon", "smile"]],
  ["🌝", ["full", "moon", "face"]],
  ["🌞", ["sun", "face"]],
];

const OBJECTS: readonly EmojiEntry[] = [
  ["💻", ["laptop", "computer"]],
  ["🖥️", ["desktop"]],
  ["⌨️", ["keyboard"]],
  ["🖱️", ["mouse"]],
  ["🖨️", ["printer"]],
  ["📱", ["phone", "mobile"]],
  ["☎️", ["phone", "landline"]],
  ["📞", ["call"]],
  ["📟", ["pager"]],
  ["📠", ["fax"]],
  ["📷", ["camera"]],
  ["📸", ["camera", "flash"]],
  ["📹", ["videocam"]],
  ["🎥", ["movie", "camera"]],
  ["📺", ["tv"]],
  ["📻", ["radio"]],
  ["💡", ["bulb", "idea"]],
  ["🔦", ["flashlight"]],
  ["🕯️", ["candle"]],
  ["🔋", ["battery"]],
  ["🔌", ["plug"]],
  ["💾", ["floppy", "save"]],
  ["💿", ["cd"]],
  ["📀", ["dvd"]],
  ["📼", ["vhs", "tape"]],
  ["🎬", ["clapper"]],
  ["📚", ["books"]],
  ["📖", ["open", "book"]],
  ["📕", ["red", "book"]],
  ["📒", ["ledger"]],
  ["📓", ["notebook"]],
  ["📔", ["notebook", "cover"]],
  ["📰", ["newspaper"]],
  ["✏️", ["pencil"]],
  ["✒️", ["pen", "nib"]],
  ["🖊️", ["pen"]],
  ["🖌️", ["paintbrush"]],
  ["🖍️", ["crayon"]],
  ["📝", ["memo", "note"]],
  ["📋", ["clipboard"]],
  ["📌", ["pushpin"]],
  ["📍", ["pin"]],
  ["📎", ["paperclip"]],
  ["🔗", ["link"]],
  ["✂️", ["scissors"]],
  ["🔑", ["key"]],
  ["🗝️", ["old", "key"]],
  ["🔒", ["lock"]],
  ["🔓", ["unlock"]],
  ["🔏", ["locked", "pen"]],
  ["🔐", ["locked", "key"]],
  ["🛒", ["cart", "shopping"]],
  ["🛍️", ["shopping", "bags"]],
  ["👑", ["crown"]],
  ["👗", ["dress"]],
  ["👙", ["bikini"]],
  ["👠", ["heels"]],
  ["👜", ["handbag"]],
  ["💄", ["lipstick"]],
  ["💍", ["ring"]],
  ["💎", ["diamond"]],
  ["⌚", ["watch"]],
  ["⏰", ["alarm"]],
  ["⏱️", ["stopwatch"]],
  ["⏳", ["hourglass"]],
];

const SYMBOLS: readonly EmojiEntry[] = [
  ["✅", ["check", "yes"]],
  ["❌", ["x", "no"]],
  ["❎", ["x", "box"]],
  ["✔️", ["check"]],
  ["☑️", ["check", "box"]],
  ["⚠️", ["warning"]],
  ["🚫", ["no", "prohibited"]],
  ["⛔", ["no", "entry"]],
  ["❗", ["exclamation"]],
  ["❓", ["question"]],
  ["❕", ["white", "exclamation"]],
  ["❔", ["white", "question"]],
  ["‼️", ["double", "exclamation"]],
  ["⁉️", ["exclamation", "question"]],
  ["♻️", ["recycle"]],
  ["⚜️", ["fleur", "lis"]],
  ["⭐", ["star"]],
  ["🌟", ["star", "glow"]],
  ["✨", ["sparkles"]],
  ["⚡", ["zap"]],
  ["💥", ["boom"]],
  ["💢", ["anger"]],
  ["💬", ["speech"]],
  ["💭", ["thought"]],
  ["🗨️", ["speech", "left"]],
  ["🗯️", ["anger", "speech"]],
  ["♥️", ["heart"]],
  ["♦️", ["diamond", "suit"]],
  ["♣️", ["club"]],
  ["♠️", ["spade"]],
  ["♾️", ["infinity"]],
  ["🆗", ["ok", "button"]],
  ["🆕", ["new"]],
  ["🆒", ["cool"]],
  ["🆓", ["free"]],
  ["🆙", ["up"]],
  ["🆘", ["sos"]],
  ["🔝", ["top"]],
  ["🔚", ["end"]],
  ["🔙", ["back"]],
  ["🔜", ["soon"]],
  ["🔛", ["on"]],
  ["🔄", ["arrows", "counterclockwise"]],
  ["🔀", ["shuffle"]],
  ["🔁", ["repeat"]],
  ["🔂", ["repeat", "one"]],
  ["▶️", ["play"]],
  ["⏸️", ["pause"]],
  ["⏹️", ["stop"]],
  ["⏺️", ["record"]],
  ["⏩", ["fast", "forward"]],
  ["⏪", ["rewind"]],
  ["⏫", ["fast", "up"]],
  ["⏬", ["fast", "down"]],
  ["🔼", ["up", "arrow"]],
  ["🔽", ["down", "arrow"]],
  ["⬆️", ["arrow", "up"]],
  ["⬇️", ["arrow", "down"]],
  ["⬅️", ["arrow", "left"]],
  ["➡️", ["arrow", "right"]],
  ["↗️", ["up", "right"]],
  ["↘️", ["down", "right"]],
  ["↙️", ["down", "left"]],
  ["↖️", ["up", "left"]],
  ["↕️", ["up", "down"]],
  ["↔️", ["left", "right"]],
];

interface Category { id: string; label: string; items: readonly EmojiEntry[] }

const CATEGORIES: Category[] = [
  { id: "flirty",     label: "💋", items: FLIRTY },
  { id: "faces",      label: "😀", items: FACES },
  { id: "gestures",   label: "👍", items: GESTURES },
  { id: "party",      label: "🎉", items: PARTY },
  { id: "animals",    label: "🐶", items: ANIMALS },
  { id: "food",       label: "🍕", items: FOOD },
  { id: "activities", label: "⚽", items: ACTIVITIES },
  { id: "travel",     label: "✈️", items: TRAVEL },
  { id: "objects",    label: "💡", items: OBJECTS },
  { id: "symbols",    label: "❗", items: SYMBOLS },
];

const ALL_ENTRIES: EmojiEntry[] = CATEGORIES.flatMap((c) => [...c.items]);

/** Backwards-compat: callers that import EMOJI_LIST get the bare strings. */
export const EMOJI_LIST: readonly string[] = ALL_ENTRIES.map(([e]) => e);

/** Insert text at the current cursor of a textarea + restore focus. */
export function insertAtCursor(
  textarea: HTMLTextAreaElement,
  current: string,
  inserted: string,
  setValue: (s: string) => void,
) {
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const next = current.slice(0, start) + inserted + current.slice(end);
  setValue(next);
  setTimeout(() => {
    textarea.focus();
    textarea.selectionStart = textarea.selectionEnd = start + inserted.length;
  }, 0);
}

function loadRecents(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(LS_RECENTS_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.slice(0, MAX_RECENTS).filter((s) => typeof s === "string") : [];
  } catch { return []; }
}

function pushRecent(emoji: string) {
  if (typeof window === "undefined") return;
  try {
    const cur = loadRecents();
    const next = [emoji, ...cur.filter((e) => e !== emoji)].slice(0, MAX_RECENTS);
    window.localStorage.setItem(LS_RECENTS_KEY, JSON.stringify(next));
    window.dispatchEvent(new CustomEvent(RECENTS_EVENT));
  } catch { /* ignore quota */ }
}

/** Build the quick-row: most-recent leftmost, then fall back to defaults
 *  for any unused slots — without duplicating emojis already in recents. */
function buildQuickRow(recents: string[], slots: number): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const e of recents) {
    if (out.length >= slots) break;
    if (seen.has(e)) continue;
    seen.add(e);
    out.push(e);
  }
  for (const e of QUICK_EMOJIS) {
    if (out.length >= slots) break;
    if (seen.has(e)) continue;
    seen.add(e);
    out.push(e);
  }
  return out;
}

export function EmojiQuickRow({
  onInsert, disabled, slots = 8,
}: { onInsert: (e: string) => void; disabled?: boolean; slots?: number }) {
  const [recents, setRecents] = useState<string[]>([]);

  // Initial hydrate + listen for pulses from other instances. We mirror
  // the native storage event (cross-tab) AND our own custom event
  // (same-tab) so a pick in the popover re-shuffles every visible row.
  useEffect(() => {
    setRecents(loadRecents());
    const refresh = () => setRecents(loadRecents());
    window.addEventListener(RECENTS_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(RECENTS_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  const row = useMemo(() => buildQuickRow(recents, slots), [recents, slots]);

  return (
    <div className="flex gap-1">
      {row.map((e) => (
        <button
          key={e}
          type="button"
          disabled={disabled}
          onClick={() => { pushRecent(e); onInsert(e); }}
          className="w-7 h-7 grid place-items-center rounded-md hover:bg-bg-elev-1 disabled:opacity-40 text-base"
        >
          {e}
        </button>
      ))}
    </div>
  );
}

export function EmojiPickerButton({
  onInsert, disabled, align = "right",
}: { onInsert: (e: string) => void; disabled?: boolean; align?: "left" | "right" }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<string>(CATEGORIES[0].id);
  const [search, setSearch] = useState("");
  const [recents, setRecents] = useState<string[]>([]);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const [coords, setCoords] = useState<{
    top?: number;
    bottom?: number;
    right?: number;
    left?: number;
    maxHeight: number;
  } | null>(null);

  // Re-load recents whenever the picker opens so we pick up clicks from
  // other components (quick-row) that also write to the store.
  useEffect(() => {
    if (open) {
      setRecents(loadRecents());
      setSearch("");
      setTimeout(() => searchInputRef.current?.focus(), 50);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      const t = e.target as Node;
      if (popoverRef.current?.contains(t)) return;
      if (popRef.current?.contains(t)) return;
      setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;
    const update = () => {
      const r = buttonRef.current?.getBoundingClientRect();
      if (!r) return;
      const PANEL_W = 384;
      const PANEL_H_PREF = 400;
      const margin = 8;
      const spaceAbove = r.top - margin;
      const spaceBelow = window.innerHeight - r.bottom - margin;
      const openDown = spaceBelow >= PANEL_H_PREF || spaceBelow > spaceAbove;
      const vert: { top?: number; bottom?: number; maxHeight: number } = openDown
        ? { top: r.bottom + 8, maxHeight: Math.min(PANEL_H_PREF, spaceBelow) }
        : { bottom: window.innerHeight - r.top + 8, maxHeight: Math.min(PANEL_H_PREF, spaceAbove) };
      if (align === "right") {
        const wantRight = window.innerWidth - r.right;
        const maxRight = window.innerWidth - PANEL_W - margin;
        setCoords({ ...vert, right: Math.max(margin, Math.min(wantRight, maxRight)) });
      } else {
        const wantLeft = r.left;
        const maxLeft = window.innerWidth - PANEL_W - margin;
        setCoords({ ...vert, left: Math.max(margin, Math.min(wantLeft, maxLeft)) });
      }
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [open, align]);

  const filtered = useMemo<string[]>(() => {
    const q = search.trim().toLowerCase();
    if (!q) return [];
    return ALL_ENTRIES
      .filter(([e, kws]) => e.includes(q) || kws.some((k) => k.toLowerCase().includes(q)))
      .map(([e]) => e);
  }, [search]);

  function pick(e: string) {
    pushRecent(e);
    onInsert(e);
    setOpen(false);
  }

  const activeCategory = CATEGORIES.find((c) => c.id === tab) ?? CATEGORIES[0];

  return (
    <div ref={popoverRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        className="w-8 h-8 grid place-items-center rounded-md hover:bg-bg-elev-1 disabled:opacity-40"
        aria-label="Emoji picker"
      >
        😊
      </button>
      {open && coords && typeof document !== "undefined" && createPortal(
        <div
          ref={popRef}
          style={{
            position: "fixed",
            top: coords.top,
            bottom: coords.bottom,
            right: coords.right,
            left: coords.left,
            maxHeight: coords.maxHeight,
          }}
          className={cn(
            "z-[60] flex flex-col",
            "bg-panel border border-border rounded-xl shadow-lg",
            "w-96 max-w-[90vw]",
          )}
        >
          <div className="p-2 border-b border-border">
            <input
              ref={searchInputRef}
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search emoji…"
              className="w-full bg-bg border border-border rounded-md px-2 py-1 text-xs placeholder:text-muted focus:outline-none focus:border-accent"
            />
          </div>

          {search.trim() ? (
            <div className="p-2 flex-1 min-h-0 overflow-y-auto">
              {filtered.length === 0 ? (
                <div className="text-xs text-fg-dim text-center py-6">
                  No emoji match &ldquo;{search}&rdquo;.
                </div>
              ) : (
                <div className="grid grid-cols-8 gap-1">
                  {filtered.map((e) => (
                    <EmojiBtn key={e} emoji={e} onClick={pick} />
                  ))}
                </div>
              )}
            </div>
          ) : (
            <>
              <div className="flex items-center gap-1 px-2 pt-2 overflow-x-auto scrollbar-thin">
                {recents.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setTab("recents")}
                    className={cn(
                      "px-2 py-1 rounded-md text-base shrink-0",
                      tab === "recents" ? "bg-bg-elev-1" : "hover:bg-bg-elev-1/50",
                    )}
                    title="Recent"
                  >
                    🕘
                  </button>
                )}
                {CATEGORIES.map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => setTab(c.id)}
                    className={cn(
                      "px-2 py-1 rounded-md text-base shrink-0",
                      tab === c.id ? "bg-bg-elev-1" : "hover:bg-bg-elev-1/50",
                    )}
                    title={c.id}
                  >
                    {c.label}
                  </button>
                ))}
              </div>
              <div className="p-2 flex-1 min-h-0 overflow-y-auto">
                {tab === "recents" && recents.length > 0 ? (
                  <div className="grid grid-cols-8 gap-1">
                    {recents.map((e, i) => (
                      <EmojiBtn key={`${e}-${i}`} emoji={e} onClick={pick} />
                    ))}
                  </div>
                ) : (
                  <div className="grid grid-cols-8 gap-1">
                    {activeCategory.items.map(([e]) => (
                      <EmojiBtn key={e} emoji={e} onClick={pick} />
                    ))}
                  </div>
                )}
              </div>
            </>
          )}
        </div>,
        document.body,
      )}
    </div>
  );
}

function EmojiBtn({ emoji, onClick }: { emoji: string; onClick: (e: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => onClick(emoji)}
      className="w-7 h-7 grid place-items-center rounded-md hover:bg-bg-elev-1 text-base"
    >
      {emoji}
    </button>
  );
}
